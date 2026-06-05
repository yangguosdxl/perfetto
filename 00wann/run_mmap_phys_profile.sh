#!/usr/bin/env bash
set -euo pipefail

export MSYS_NO_PATHCONV=1
export PERFETTO_SYMBOLIZER_MODE=index
export PERFETTO_BINARY_PATH='./workspace/allsymbols/arm64-v8a'
source config.sh
export PATH="$PerfettoRoot/buildtools/linux64/clang/bin:$PATH"

app=${MMAP_PHYS_APP:-com.tencent.dhwdxkty.trunk.profiler}
trace_processor=${TRACE_PROCESSOR:-$PerfettoRoot/out/linux_clang_release/trace_processor_shell}
traceconv=${TRACECONV:-$PerfettoRoot/out/linux_clang_release/traceconv}

source fsbootcmd_push_to_phone.sh
adb push debugconfig.txt /sdcard/Android/data/$app/files

python3 collect_mmap_phys_data.py \
  --name "$app" \
  --trace-processor "$trace_processor" \
  --traceconv "$traceconv" \
  "$@"
