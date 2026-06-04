#!/usr/bin/env bash
# 在BAT里运行时，会出现符号无法正确解析的问题
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir" || exit

# shellcheck source=00wann/config.sh
source config.sh

export MSYS_NO_PATHCONV=1
export PERFETTO_SYMBOLIZER_MODE=index 
export PERFETTO_BINARY_PATH='./workspace/allsymbols/arm64-v8a'
export PATH="$PerfettoRoot/buildtools/linux64/clang/bin:$PATH"
export PYTHONPATH="$PerfettoRoot/python${PYTHONPATH:+:$PYTHONPATH}"
traceconv_binary="$PerfettoRoot/out/linux_clang_release/traceconv"
trace_processor_binary="$PerfettoRoot/out/linux_clang_release/trace_processor_shell"
duration_ms="${1:-}"
duration_args=()
if [ "$duration_ms" != "" ]; then
    # AI 真机验证时传 45000，让采集自动收尾；人工采集默认不限制时长。
    duration_args=(-d "$duration_ms")
fi
interval_bytes="${2:-16}"
interval_args=()
if [ "$interval_bytes" != "" ]; then
    # interval 单位为 bytes；默认 16，用于高密度采集启动阶段分配栈。
    interval_args=(-i "$interval_bytes")
fi
shmem_size="${3:-8388608}"
shmem_args=()
if [ "$shmem_size" != "" ]; then
    # shmem-size 单位为 bytes；默认 8M，命令行第 3 个参数可覆盖。
    shmem_args=(--shmem-size "$shmem_size")
fi
dir="PerfData/mem/$(date +%F_%k-%M-%S)"
if [ ! -d "$dir" ]
then
    mkdir -p "$dir"
fi

# shellcheck source=00wann/fsbootcmd_push_to_phone.sh
source fsbootcmd_push_to_phone.sh

app=com.tencent.dhwdxkty.trunk.profiler
adb push debugconfig.txt "/sdcard/Android/data/$app/files"

pid=""
launch_attempted=0
while [ "$pid" == "" ]
do
    pid=$(adb shell pidof "$app")
    if [ "$pid" == "" ] && [ "$launch_attempted" -eq 0 ]; then
        # 这里只发送一次启动 Intent，用于拉起目标应用，不用于触发随机测试场景。
        printf "\n目标进程未启动，尝试启动应用: %s\n" "$app"
        adb shell monkey -p "$app" 1
        launch_attempted=1
    fi
    printf "\r等待应用启动: %s ..." "$app"
    sleep 0.2
done
printf "\r应用已启动: %s pid=%s\n" "$app" "$pid"

cp "$traceconv_binary" ~/.local/share/perfetto/prebuilts/traceconv
python3 "$PerfettoRoot/python/tools/heap_profile.py" \
    -n "$app" \
    -o "$dir" \
    --traceconv-binary "$traceconv_binary" \
    --trace-processor-binary "$trace_processor_binary" \
    "${duration_args[@]}" \
    "${interval_args[@]}" \
    "${shmem_args[@]}"
