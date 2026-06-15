#!/usr/bin/env bash
set -euo pipefail

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

script_dir=$(cd "$(dirname "$0")" && pwd)
cp "$script_dir/run_mmap_phys_analyze_latest.sh" "$tmpdir/run_mmap_phys_analyze_latest.sh"
cp "$script_dir/common_tools.sh" "$tmpdir/common_tools.sh"

cat >"$tmpdir/config.sh" <<EOF
PerfettoRoot="$tmpdir/perfetto"
EOF

mkdir -p "$tmpdir/heap_analyzer"
cp "$script_dir/heap_analyzer/fs.ini" "$tmpdir/heap_analyzer/fs.ini"

mkdir -p "$tmpdir/perfetto/out/linux_clang_release" "$tmpdir/bin"
cat >"$tmpdir/perfetto/out/linux_clang_release/trace_processor_shell" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'trace_processor %s\n' "$*" >>"${TEST_LOG:?}"
printf '"pid"\n2468\n'
EOF
cat >"$tmpdir/bin/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'python3 %s\n' "$*" >>"${TEST_LOG:?}"
EOF
chmod +x "$tmpdir/perfetto/out/linux_clang_release/trace_processor_shell" "$tmpdir/bin/python3"

mkdir -p \
  "$tmpdir/PerfData/mmap_phys/2026-06-05_10-08-27/smaps" \
  "$tmpdir/PerfData/mmap_phys/2026-06-05_17-40-36/smaps"
touch -t 202606051008 \
  "$tmpdir/PerfData/mmap_phys/2026-06-05_10-08-27/mmap_trace.perfetto-trace"
touch -t 202606051740 \
  "$tmpdir/PerfData/mmap_phys/2026-06-05_17-40-36/mmap_trace.perfetto-trace" \
  "$tmpdir/PerfData/mmap_phys/2026-06-05_17-40-36/symbolized-trace"
touch "$tmpdir/mmap_phys_analyzer.py"

export PATH="$tmpdir/bin:$PATH"
export TEST_LOG="$tmpdir/commands.log"

cd "$tmpdir"
./run_mmap_phys_analyze_latest.sh --top-n 25 >"$tmpdir/default.out"

if ! grep -Fq -- "trace_processor query PerfData/mmap_phys/2026-06-05_17-40-36/symbolized-trace" "$TEST_LOG"; then
  echo "wrapper 应使用最近目录中的 symbolized-trace 自动查询 pid"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--trace PerfData/mmap_phys/2026-06-05_17-40-36/symbolized-trace" "$TEST_LOG"; then
  echo "wrapper 应默认使用最近抓取目录中的 symbolized-trace"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--smaps-dir PerfData/mmap_phys/2026-06-05_17-40-36/smaps" "$TEST_LOG"; then
  echo "wrapper 应默认使用最近抓取目录中的 smaps"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--pid 2468" "$TEST_LOG"; then
  echo "wrapper 应在未传 --pid 时自动补齐 trace 中的目标 pid"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--classify-config heap_analyzer/fs.ini" "$TEST_LOG"; then
  echo "wrapper 应默认指定 fs.ini 分类配置"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--classify-speedscope-dir mmap_categories" "$TEST_LOG"; then
  echo "wrapper 应默认输出每个分类的 speedscope 文件"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--top-n 0 --top-n 25" "$TEST_LOG"; then
  echo "wrapper 应把用户参数放在默认参数之后，允许覆盖默认 top-n"
  cat "$TEST_LOG"
  exit 1
fi

: >"$TEST_LOG"
./run_mmap_phys_analyze_latest.sh \
  --pid 1357 \
  --trace PerfData/mmap_phys/2026-06-05_10-08-27/mmap_trace.perfetto-trace \
  --smaps-dir PerfData/mmap_phys/2026-06-05_10-08-27/smaps \
  --output custom.json >"$tmpdir/override.out"

if grep -Fq -- "trace_processor query" "$TEST_LOG"; then
  echo "显式传入 --pid 时 wrapper 不应再查询 trace_processor"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--pid 1357" "$TEST_LOG"; then
  echo "显式 --pid 应保留在默认 pid 之后生效"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--trace PerfData/mmap_phys/2026-06-05_17-40-36/symbolized-trace" "$TEST_LOG"; then
  echo "wrapper 仍应提供最近 trace 作为默认值"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--trace PerfData/mmap_phys/2026-06-05_10-08-27/mmap_trace.perfetto-trace" "$TEST_LOG"; then
  echo "用户显式 --trace 应追加在默认 trace 之后以覆盖默认值"
  cat "$TEST_LOG"
  exit 1
fi
