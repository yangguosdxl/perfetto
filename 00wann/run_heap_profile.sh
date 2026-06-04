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
export PYTHONUNBUFFERED=1
traceconv_binary="$PerfettoRoot/out/linux_clang_release/traceconv"
trace_processor_binary="$PerfettoRoot/out/linux_clang_release/trace_processor_shell"
malloc_live_sql="select coalesce(sum(size), 0) as malloc_live_bytes from heap_profile_allocation;"
health_sql="select coalesce(sum(value), 0) as health_sum from stats where name in ('traced_buf_bytes_overwritten','traced_buf_chunks_overwritten','traced_buf_chunks_discarded','traced_buf_trace_writer_packet_loss','traced_buf_patches_failed','traced_buf_abi_violations','heapprofd_buffer_overran','heapprofd_client_error','heapprofd_missing_packet','heapprofd_non_finalized_profile');"
duration_ms="${1:-}"
duration_args=()
if [ "$duration_ms" != "" ]; then
    # AI 真机验证时传 45000，让采集自动收尾；人工采集默认不限制时长。
    duration_args=(-d "$duration_ms")
fi
interval_bytes="${2:-1024}"
interval_args=()
if [ "$interval_bytes" != "" ]; then
    # interval 单位为 bytes；默认 1024，用于降低 malloc live 与 meminfo 账面分配的估算偏差。
    interval_args=(-i "$interval_bytes")
fi
shmem_size="${3:-8388608}"
shmem_args=()
if [ "$shmem_size" != "" ]; then
    # shmem-size 单位为 bytes；默认 8M，命令行第 3 个参数可覆盖。
    shmem_args=(--shmem-size "$shmem_size")
fi

query_trace_value() {
    local query=$1
    local trace_path=$2
    "$trace_processor_binary" -Q "$query" "$trace_path" 2>/dev/null | tail -1 | tr -d '"' | tr -d '\r'
}

parse_meminfo_native_heap_alloc_bytes() {
    local meminfo_path=$1
    # Native Heap 行最后三列是 Heap Size/Alloc/Free，这里只取 Heap Alloc 做账面分配口径对比。
    awk '
        $1 == "Native" && $2 == "Heap" && $9 ~ /^-?[0-9,]+$/ {
            gsub(",", "", $9)
            printf "%.0f\n", $9 * 1024
            found = 1
            exit
        }
        END {
            if (!found) {
                exit 1
            }
        }
    ' "$meminfo_path"
}

capture_meminfo_snapshot() {
    local out_dir=$1
    local package_name=$2
    local meminfo_path="$out_dir/dumpsys_meminfo.txt"
    local tmp_path="$meminfo_path.tmp"
    printf "\n抓取采集后 meminfo: adb shell dumpsys meminfo %s\n" "$package_name"
    if adb shell dumpsys meminfo "$package_name" >"$tmp_path"; then
        mv "$tmp_path" "$meminfo_path"
        return 0
    fi
    rm -f "$tmp_path"
    return 1
}

abs_int() {
    local value=$1
    if [ "$value" -lt 0 ]; then
        printf '%s\n' $(( -value ))
    else
        printf '%s\n' "$value"
    fi
}

validate_heap_profile_against_meminfo() {
    local out_dir=$1
    local package_name=$2
    local meminfo_path="$out_dir/dumpsys_meminfo.txt"
    local validation_path="$out_dir/heap_meminfo_validation.txt"
    local trace_path="$out_dir/symbolized-trace"
    if [ ! -f "$trace_path" ]; then
        trace_path="$out_dir/raw-trace"
    fi

    if [ ! -f "$meminfo_path" ] && ! capture_meminfo_snapshot "$out_dir" "$package_name"; then
        printf 'HEAP_MEMINFO_VALIDATION=FAIL|reason=dumpsys_meminfo_failed|path=%s\n' "$meminfo_path" | tee "$validation_path"
        return 1
    fi

    if [ ! -f "$trace_path" ]; then
        printf 'HEAP_MEMINFO_VALIDATION=FAIL|reason=trace_missing|checked=%s\n' "$trace_path" | tee "$validation_path"
        return 1
    fi

    local meminfo_alloc_bytes
    if ! meminfo_alloc_bytes=$(parse_meminfo_native_heap_alloc_bytes "$meminfo_path"); then
        printf 'HEAP_MEMINFO_VALIDATION=FAIL|reason=parse_native_heap_alloc_failed|meminfo=%s\n' "$meminfo_path" | tee "$validation_path"
        return 1
    fi

    # 主判断只使用 heap_profile_allocation 全窗口净值，避免把 latest dump 分片误当作 malloc live 总量。
    local malloc_live_bytes
    malloc_live_bytes=$(query_trace_value "$malloc_live_sql" "$trace_path")
    if ! printf '%s\n' "$malloc_live_bytes" | grep -Eq '^-?[0-9]+$'; then
        printf 'HEAP_MEMINFO_VALIDATION=FAIL|reason=query_malloc_live_failed|trace=%s|value=%s\n' "$trace_path" "$malloc_live_bytes" | tee "$validation_path"
        return 1
    fi

    local health_sum
    health_sum=$(query_trace_value "$health_sql" "$trace_path")
    if ! printf '%s\n' "$health_sum" | grep -Eq '^-?[0-9]+$'; then
        health_sum="unknown"
    fi

    local diff_bytes
    diff_bytes=$(abs_int $((malloc_live_bytes - meminfo_alloc_bytes)))
    local allowed_diff_bytes="${HEAP_PROFILE_MEMINFO_ALLOWED_DIFF_BYTES:-67108864}"
    local heap_dump_count
    heap_dump_count=$(find "$out_dir" -maxdepth 1 -type f -name 'heap_dump.*.pb.gz' | wc -l | tr -d ' ')

    {
        printf 'malloc_live_bytes=%s\n' "$malloc_live_bytes"
        printf 'meminfo_native_heap_alloc_bytes=%s\n' "$meminfo_alloc_bytes"
        printf 'diff_bytes=%s\n' "$diff_bytes"
        printf 'allowed_diff_bytes=%s\n' "$allowed_diff_bytes"
        printf 'health_sum=%s\n' "$health_sum"
        printf 'heap_dump_count=%s\n' "$heap_dump_count"
        printf 'trace_path=%s\n' "$trace_path"
        printf 'meminfo_path=%s\n' "$meminfo_path"
        printf 'live_sql=%s\n' "$malloc_live_sql"
    } | tee "$validation_path"

    if [ "$diff_bytes" -le "$allowed_diff_bytes" ]; then
        printf 'HEAP_MEMINFO_VALIDATION=PASS|malloc_live_bytes=%s|meminfo_native_heap_alloc_bytes=%s|diff_bytes=%s|allowed_diff_bytes=%s\n' \
            "$malloc_live_bytes" "$meminfo_alloc_bytes" "$diff_bytes" "$allowed_diff_bytes" | tee -a "$validation_path"
        return 0
    fi

    printf 'HEAP_MEMINFO_VALIDATION=FAIL|reason=malloc_live_not_comparable_to_meminfo_alloc|malloc_live_bytes=%s|meminfo_native_heap_alloc_bytes=%s|diff_bytes=%s|allowed_diff_bytes=%s|health_sum=%s|heap_dump_count=%s\n' \
        "$malloc_live_bytes" "$meminfo_alloc_bytes" "$diff_bytes" "$allowed_diff_bytes" "$health_sum" "$heap_dump_count" | tee -a "$validation_path"
    return 1
}

wait_for_profiling_active() {
    local profile_log=$1
    local profiler_pid=$2
    local timeout_s="${HEAP_PROFILE_ACTIVE_TIMEOUT_S:-60}"
    local max_checks=$((timeout_s * 2))
    local i
    for i in $(seq 1 "$max_checks"); do
        if grep -q 'Profiling active' "$profile_log"; then
            return 0
        fi
        if ! kill -0 "$profiler_pid" 2>/dev/null; then
            return 1
        fi
        sleep 0.5
    done
    return 1
}

wait_for_profiler_shutdown() {
    local profile_log=$1
    local profiler_pid=$2
    local timeout_s="${HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S:-600}"
    local max_checks=$((timeout_s * 2))
    local i
    for i in $(seq 1 "$max_checks"); do
        if grep -q 'Waiting for profiler shutdown' "$profile_log"; then
            return 0
        fi
        if ! kill -0 "$profiler_pid" 2>/dev/null; then
            return 1
        fi
        sleep 0.5
    done
    return 1
}

dir="PerfData/mem/$(date +%F_%k-%M-%S)"
if [ ! -d "$dir" ]
then
    mkdir -p "$dir"
fi

# shellcheck source=00wann/fsbootcmd_push_to_phone.sh
source fsbootcmd_push_to_phone.sh

app=com.tencent.dhwdxkty.trunk.profiler
adb push debugconfig.txt "/sdcard/Android/data/$app/files"

active_profiler_pid=""
cleanup_profiler() {
    if [ "$active_profiler_pid" != "" ] && kill -0 "$active_profiler_pid" 2>/dev/null; then
        kill -INT "$active_profiler_pid" 2>/dev/null || true
        wait "$active_profiler_pid" 2>/dev/null || true
    fi
}
trap 'cleanup_profiler; exit 130' INT TERM

printf "\n重启目标应用以覆盖启动后的 native malloc 分配: %s\n" "$app"
adb shell am force-stop "$app" || true
sleep 1

pid=""

cp "$traceconv_binary" ~/.local/share/perfetto/prebuilts/traceconv
profile_log="/tmp/run_heap_profile_$(date +%s)_$$.log"
profile_log_dest="$dir/heap_profile.log"
: >"$profile_log"
python3 "$PerfettoRoot/python/tools/heap_profile.py" \
    -n "$app" \
    -o "$dir" \
    --no-running \
    --traceconv-binary "$traceconv_binary" \
    --trace-processor-binary "$trace_processor_binary" \
    "${duration_args[@]}" \
    "${interval_args[@]}" \
    "${shmem_args[@]}" > >(tee "$profile_log") 2>&1 &
active_profiler_pid=$!

if ! wait_for_profiling_active "$profile_log" "$active_profiler_pid"; then
    cp "$profile_log" "$profile_log_dest" 2>/dev/null || true
    printf 'HEAP_PROFILE_FAILED|reason=profiling_active_timeout|out_dir=%s|log=%s\n' "$dir" "$profile_log_dest"
    cleanup_profiler
    exit 1
fi

# 这里只发送一次启动 Intent，用于拉起目标应用，不用于触发随机测试场景。
printf "\nheapprofd 已就绪，启动目标应用: %s\n" "$app"
adb shell monkey -p "$app" 1
while [ "$pid" == "" ]
do
    pid=$(adb shell pidof "$app" | tr -d '\r')
    printf "\r等待应用启动: %s ..." "$app"
    sleep 0.2
done
printf "\r应用已启动: %s pid=%s\n" "$app" "$pid"

if wait_for_profiler_shutdown "$profile_log" "$active_profiler_pid"; then
    capture_meminfo_snapshot "$dir" "$app" || true
fi

wait "$active_profiler_pid"
heap_profile_rc=$?
active_profiler_pid=""
cp "$profile_log" "$profile_log_dest" 2>/dev/null || true
validation_rc=0
validate_heap_profile_against_meminfo "$dir" "$app" || validation_rc=$?
if [ "$heap_profile_rc" -ne 0 ]; then
    printf 'HEAP_PROFILE_FAILED|rc=%s|out_dir=%s\n' "$heap_profile_rc" "$dir"
    exit "$heap_profile_rc"
fi
exit "$validation_rc"
