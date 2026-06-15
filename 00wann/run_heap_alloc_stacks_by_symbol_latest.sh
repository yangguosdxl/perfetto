#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"
source common_tools.sh

usage() {
  cat <<'EOF'
用法:
  ./run_heap_alloc_stacks_by_symbol_latest.sh [query_heap_alloc_stacks_by_symbol.py 参数...]

默认行为:
  - 自动选择 PerfData/mem 下最近一次 Native heap trace
  - 优先使用最近采集目录里的 symbolized-trace
  - 默认追加 --classify-config heap_analyzer/fs.ini --all-allocations --limit 0

常用覆盖:
  ./run_heap_alloc_stacks_by_symbol_latest.sh --limit 25
  ./run_heap_alloc_stacks_by_symbol_latest.sh --trace /path/to/symbolized-trace
  ./run_heap_alloc_stacks_by_symbol_latest.sh --symbol malloc --limit 50

环境变量:
  HEAP_PROFILE_DATA_DIR=PerfData/mem   覆盖默认 trace 搜索目录
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

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

host_python_wants_windows_paths() {
  local python_bin=$1
  if ! is_windows_git_bash; then
    return 1
  fi
  case "$python_bin" in
    /[a-zA-Z]/*|/[a-zA-Z]|[a-zA-Z]:/*|[a-zA-Z]:\\*)
      return 0
      ;;
  esac
  return 1
}

python_script_path_for_host() {
  local python_bin=$1
  local script_path=$2
  # Windows 原生 Python 不能稳定识别 Git Bash 的 /d/... 脚本路径，先转成盘符路径。
  if host_python_wants_windows_paths "$python_bin" &&
      command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$script_path"
  else
    printf '%s\n' "$script_path"
  fi
}

find_latest_capture_dir() {
  local data_dir=${HEAP_PROFILE_DATA_DIR:-PerfData/mem}
  [[ -d "$data_dir" ]] || return 1

  # 先按 trace 文件更新时间找最近采集目录；进入目录后再优先选 symbolized-trace。
  find "$data_dir" -mindepth 2 -maxdepth 2 -type f \
    \( -name symbolized-trace -o -name raw-trace -o -name '*.perfetto-trace' \) \
    -printf '%T@ %h\n' 2>/dev/null |
    sort -nr |
    while read -r _ dir; do
      printf '%s\n' "$dir"
      break
    done
}

latest_perfetto_trace_in_dir() {
  local capture_dir=$1
  find "$capture_dir" -maxdepth 1 -type f -name '*.perfetto-trace' \
    -printf '%T@ %p\n' 2>/dev/null |
    sort -nr |
    while read -r _ path; do
      printf '%s\n' "$path"
      break
    done
}

select_trace_path() {
  local capture_dir=$1
  if [[ -f "$capture_dir/symbolized-trace" ]]; then
    printf '%s\n' "$capture_dir/symbolized-trace"
  elif [[ -f "$capture_dir/raw-trace" ]]; then
    printf '%s\n' "$capture_dir/raw-trace"
  else
    latest_perfetto_trace_in_dir "$capture_dir"
  fi
}

latest_dir=$(find_latest_capture_dir || true)
if [[ -z "$latest_dir" ]]; then
  echo "FATAL: 找不到可分析的 Native heap trace 目录: ${HEAP_PROFILE_DATA_DIR:-PerfData/mem}" >&2
  exit 1
fi

latest_trace=$(select_trace_path "$latest_dir")
if [[ -z "$latest_trace" ]]; then
  echo "FATAL: 最近目录中没有可分析的 trace: $latest_dir" >&2
  exit 1
fi

python_bin=$(select_python)
python_script=$(python_script_path_for_host \
  "$python_bin" \
  "$script_dir/heap_analyzer/query_heap_alloc_stacks_by_symbol.py")

cmd=(
  "$python_bin" -u -B "$python_script"
  --trace "$latest_trace"
  --classify-config heap_analyzer/fs.ini
)

if ! has_arg "--symbol" "$@"; then
  # 默认做全量分类；显式 --symbol 时不强塞全量模式，保留按符号查询能力。
  cmd+=(--all-allocations)
fi

cmd+=(
  --limit 0
  "$@"
)

echo "最近 Native heap 目录: $latest_dir"
echo "默认 trace: $latest_trace"
exec "${cmd[@]}"
