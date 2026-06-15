#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"

export MSYS_NO_PATHCONV=1
export PERFETTO_SYMBOLIZER_MODE=index
export PERFETTO_BINARY_PATH='./workspace/allsymbols/arm64-v8a'
source config.sh
source common_tools.sh
export PATH="$PerfettoRoot/buildtools/linux64/clang/bin:$PATH"

app=${MMAP_PHYS_APP:-com.tencent.dhwdxkty.trunk.profiler}
python_bin=$(select_python)
trace_processor=$(select_perfetto_tool trace_processor_shell "$PerfettoRoot" "${TRACE_PROCESSOR:-}")
traceconv=$(select_perfetto_tool traceconv "$PerfettoRoot" "${TRACECONV:-}" || true)
# 默认启用 fs.ini 分类，并输出全部调用栈，方便直接生成完整归因结果。
default_analyzer_args=(--classify-config heap_analyzer/fs.ini --top-n 0)

source fsbootcmd_push_to_phone.sh
adb push debugconfig.txt /sdcard/Android/data/$app/files

"$python_bin" collect_mmap_phys_data.py \
  --name "$app" \
  --trace-processor "$trace_processor" \
  --traceconv "$traceconv" \
  "${default_analyzer_args[@]}" \
  "$@"
