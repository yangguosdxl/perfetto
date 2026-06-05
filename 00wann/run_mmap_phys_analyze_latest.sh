#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"

usage() {
  cat <<'EOF'
用法:
  ./run_mmap_phys_analyze_latest.sh [mmap_phys_analyzer.py 参数...]

默认行为:
  - 自动选择 PerfData/mmap_phys 下最近一次可分析采集目录
  - 优先使用 symbolized-trace，缺失时回退 mmap_trace.perfetto-trace
  - 默认追加 --classify-config heap_analyzer/fs.ini
  - 未传 --pid 时按 MMAP_PHYS_APP 从 trace 中查询目标进程 pid

常用覆盖:
  MMAP_PHYS_APP=com.example.app ./run_mmap_phys_analyze_latest.sh
  ./run_mmap_phys_analyze_latest.sh --pid 1234 --top-n 25
  ./run_mmap_phys_analyze_latest.sh --trace <trace> --smaps-dir <smaps> --pid <pid>
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ -f config.sh ]]; then
  # config.sh 提供 PerfettoRoot，保持和采集入口一致的 trace_processor 默认路径。
  # shellcheck disable=SC1091
  source config.sh
fi

get_arg_value() {
  local name=$1
  shift
  local value=
  while (($#)); do
    case "$1" in
      "$name")
        shift
        if (($#)); then
          value=$1
        fi
        ;;
      "$name"=*)
        value=${1#*=}
        ;;
    esac
    shift || true
  done
  [[ -n "$value" ]] || return 1
  printf '%s\n' "$value"
}

has_arg() {
  local name=$1
  shift
  while (($#)); do
    case "$1" in
      "$name"|"$name"=*)
        return 0
        ;;
    esac
    shift
  done
  return 1
}

find_latest_capture_dir() {
  local data_dir=${MMAP_PHYS_DATA_DIR:-PerfData/mmap_phys}
  [[ -d "$data_dir" ]] || return 1

  find "$data_dir" -mindepth 2 -maxdepth 2 -type f \
    \( -name symbolized-trace -o -name mmap_trace.perfetto-trace \) \
    -printf '%T@ %h\n' 2>/dev/null |
    sort -nr |
    while read -r _ dir; do
      if [[ -d "$dir/smaps" ]]; then
        printf '%s\n' "$dir"
        break
      fi
    done
}

select_trace_path() {
  local capture_dir=$1
  if [[ -f "$capture_dir/symbolized-trace" ]]; then
    printf '%s\n' "$capture_dir/symbolized-trace"
  elif [[ -f "$capture_dir/mmap_trace.perfetto-trace" ]]; then
    printf '%s\n' "$capture_dir/mmap_trace.perfetto-trace"
  else
    return 1
  fi
}

query_target_pid() {
  local trace_processor=$1
  local trace_path=$2
  local app=$3
  local app_sql=${app//\'/\'\'}
  local sql
  sql=$(cat <<SQL
WITH process_pids AS (
  SELECT pid
  FROM __intrinsic_process
  WHERE name = '$app_sql'
    AND pid IS NOT NULL
    AND pid != 0
),
event_counts AS (
  SELECT IFNULL(pr.pid, th.tid) AS pid, COUNT(1) AS cnt
  FROM __intrinsic_ftrace_event fe
  JOIN __intrinsic_thread th ON fe.utid = th.id
  LEFT JOIN __intrinsic_process pr ON th.upid = pr.id
  WHERE fe.name IN (
    'raw_syscalls/sys_enter',
    'raw_syscalls/sys_exit',
    'sys_enter',
    'sys_exit'
  )
  GROUP BY 1
)
SELECT p.pid AS pid
FROM process_pids p
LEFT JOIN event_counts e ON p.pid = e.pid
ORDER BY IFNULL(e.cnt, 0) DESC, p.pid DESC
LIMIT 1
SQL
)
  "$trace_processor" query "$trace_path" "$sql" |
    awk -F',' '
      {
        value = $1
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
        gsub(/^"|"$/, "", value)
        if (value ~ /^[0-9]+$/) {
          print value
          exit
        }
      }'
}

latest_dir=$(find_latest_capture_dir || true)
if [[ -z "$latest_dir" ]]; then
  echo "FATAL: 找不到可分析的 mmap 采集目录: ${MMAP_PHYS_DATA_DIR:-PerfData/mmap_phys}" >&2
  exit 1
fi

latest_trace=$(select_trace_path "$latest_dir")
latest_smaps_dir="$latest_dir/smaps"
latest_output="$latest_dir/mmap_phys_attribution.json"
latest_speedscope_output="$latest_dir/mmap_phys_attribution.speedscope.json"

trace_processor=${TRACE_PROCESSOR:-}
if [[ -z "$trace_processor" && -n "${PerfettoRoot:-}" ]]; then
  trace_processor="$PerfettoRoot/out/linux_clang_release/trace_processor_shell"
fi
user_trace_processor=$(get_arg_value "--trace-processor" "$@" || true)
if [[ -n "$user_trace_processor" ]]; then
  trace_processor="$user_trace_processor"
fi

effective_trace=$(get_arg_value "--trace" "$@" || true)
if [[ -z "$effective_trace" ]]; then
  effective_trace="$latest_trace"
fi

auto_pid=
if ! has_arg "--pid" "$@"; then
  app=${MMAP_PHYS_APP:-com.tencent.dhwdxkty.trunk.profiler}
  if [[ -z "$trace_processor" || ! -x "$trace_processor" ]]; then
    echo "FATAL: 未传 --pid，且找不到可执行 trace_processor_shell: ${trace_processor:-<empty>}" >&2
    echo "提示: 可设置 TRACE_PROCESSOR，或直接传 --pid <目标进程 pid>" >&2
    exit 1
  fi
  echo "查询目标 pid: app=$app trace=$effective_trace"
  auto_pid=$(query_target_pid "$trace_processor" "$effective_trace" "$app")
  if [[ -z "$auto_pid" ]]; then
    echo "FATAL: 无法从 trace 中查询目标 pid: app=$app" >&2
    echo "提示: 设置 MMAP_PHYS_APP=<进程名>，或直接传 --pid <目标进程 pid>" >&2
    exit 1
  fi
fi

cmd=(
  python3 -u -B "$script_dir/mmap_phys_analyzer.py"
  --trace "$latest_trace"
  --smaps-dir "$latest_smaps_dir"
)
if [[ -n "$auto_pid" ]]; then
  cmd+=(--pid "$auto_pid")
fi
cmd+=(
  --output "$latest_output"
  --speedscope-output "$latest_speedscope_output"
)
if [[ -n "$trace_processor" ]]; then
  cmd+=(--trace-processor "$trace_processor")
fi
cmd+=(
  --classify-config heap_analyzer/fs.ini
  --classify-speedscope-dir mmap_categories
  --top-n 0
  "$@"
)

echo "最近 mmap 目录: $latest_dir"
echo "默认 trace: $latest_trace"
echo "默认 smaps: $latest_smaps_dir"
if [[ -n "$auto_pid" ]]; then
  echo "自动 pid: $auto_pid"
else
  echo "目标 pid: 使用用户传入参数"
fi
exec "${cmd[@]}"
