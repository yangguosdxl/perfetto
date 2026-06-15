#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir" || exit

# shellcheck source=00wann/config.sh
source config.sh
source common_tools.sh

app=${HEAP_STARTUP_APP:-com.tencent.dhwdxkty.trunk.profiler}
activity=${HEAP_STARTUP_ACTIVITY:-com.tencent.dhwdxkty.trunk.profiler/com.dhplugin.unity.MainActivity}
pattern=${HEAP_STARTUP_PATTERN:-LAN 更新流程开始}
wait_timeout_s=${HEAP_STARTUP_WAIT_TIMEOUT_S:-90}
duration_ms="${1:-45000}"
shmem_size="${2:-268435456}"
intervals=()
if [ "$#" -gt 2 ]; then
    shift 2
    intervals=("$@")
else
    intervals=(512 256 128 64 32 16)
fi

export MSYS_NO_PATHCONV=1
export PERFETTO_SYMBOLIZER_MODE=index
export PERFETTO_BINARY_PATH='./workspace/allsymbols/arm64-v8a'
export PATH="$PerfettoRoot/buildtools/linux64/clang/bin:$PATH"
export PYTHONPATH="$PerfettoRoot/python${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
health_sql="select coalesce(sum(value), 0) as health_sum from stats where name in ('traced_buf_bytes_overwritten','traced_buf_chunks_overwritten','traced_buf_chunks_discarded','traced_buf_trace_writer_packet_loss','traced_buf_patches_failed','traced_buf_abi_violations','heapprofd_buffer_overran','heapprofd_client_error','heapprofd_missing_packet','heapprofd_non_finalized_profile');"
alloc_sql="select count(*) as alloc_rows, sum(case when size > 0 then 1 else 0 end) as positive_rows, sum(size) as net_size from heap_profile_allocation;"

active_logcat_pid=""
active_profiler_pid=""

cleanup() {
    if [ "$active_logcat_pid" != "" ]; then
        kill "$active_logcat_pid" 2>/dev/null || true
    fi
    if [ "$active_profiler_pid" != "" ]; then
        kill -INT "$active_profiler_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

print_config() {
    printf 'CONFIG|duration_ms=%s|shmem_size=%s|intervals=%s\n' \
        "$duration_ms" "$shmem_size" "${intervals[*]}"
    printf 'BASELINE|pattern=%s|app=%s|activity=%s\n' "$pattern" "$app" "$activity"
    local interval
    for interval in "${intervals[@]}"; do
        printf 'CASE|interval=%s|duration_ms=%s|shmem_size=%s\n' \
            "$interval" "$duration_ms" "$shmem_size"
    done
}

if [ "${HEAP_STARTUP_DRY_RUN:-0}" = "1" ]; then
    print_config
    exit 0
fi

python_bin=$(select_python)
traceconv_binary=$(select_perfetto_tool traceconv "$PerfettoRoot" "${TRACECONV:-}" || true)
trace_processor_binary=$(select_perfetto_tool trace_processor_shell "$PerfettoRoot" "${TRACE_PROCESSOR:-}")

lan_elapsed_ms="MISSING"
lan_line=""

measure_lan_startup() {
    local label=$1
    local logcat_file="/tmp/heap_startup_${label}.logcat.log"
    local start_ms
    local found=0
    lan_elapsed_ms="MISSING"
    lan_line=""

    : >"$logcat_file"
    adb logcat -v epoch >"$logcat_file" &
    active_logcat_pid=$!
    sleep 0.5
    start_ms=$(adb shell date +%s%3N | tr -d '\r')
    adb shell am start -n "$activity" >"/tmp/heap_startup_${label}.am_start.out"

    for _ in $(seq 1 $((wait_timeout_s * 10))); do
        if rg -q "$pattern" "$logcat_file"; then
            found=1
            break
        fi
        sleep 0.1
    done

    kill "$active_logcat_pid" 2>/dev/null || true
    wait "$active_logcat_pid" 2>/dev/null || true
    active_logcat_pid=""

    if [ "$found" -ne 1 ]; then
        return 1
    fi

    lan_line=$(rg "$pattern" "$logcat_file" | head -1)
    local end_sec
    local end_ms
    end_sec=$(printf '%s\n' "$lan_line" | awk '{print $1}')
    end_ms=$(awk -v seconds="$end_sec" 'BEGIN { printf "%.0f", seconds * 1000 }')
    lan_elapsed_ms=$((end_ms - start_ms))
    return 0
}

run_baseline() {
    adb shell am force-stop "$app" || true
    sleep 2
    adb logcat -c
    if measure_lan_startup "baseline"; then
        printf 'RESULT|baseline|LAN_STARTUP_MS=%s|LOG_LINE=%s\n' \
            "$lan_elapsed_ms" "$lan_line"
    else
        printf 'RESULT|baseline|LAN_STARTUP_MS=MISSING|LOG_LINE=\n'
    fi
}

query_trace_value() {
    local query=$1
    local raw_trace=$2
    "$trace_processor_binary" -Q "$query" "$raw_trace" 2>/dev/null | tail -1 | tr -d '"'
}

run_profile_case() {
    local interval=$1
    local label="lan_i${interval}_s$((shmem_size / 1048576))m"
    local out_dir
    out_dir="PerfData/mem/startup_eval_$(date +%F_%H-%M-%S)_${label}"
    local prof_log="/tmp/heap_startup_${label}.prof.log"
    local raw_trace="${out_dir}/raw-trace"
    local heap_count=0
    local alloc_line="alloc_query_failed"
    local health_sum="unknown"

    mkdir -p "$out_dir"
    adb shell am force-stop "$app" || true
    sleep 2
    adb logcat -c

    "$python_bin" "$PerfettoRoot/python/tools/heap_profile.py" \
        -n "$app" \
        -o "$out_dir" \
        -d "$duration_ms" \
        -i "$interval" \
        --shmem-size "$shmem_size" \
        --no-running \
        --traceconv-binary "$traceconv_binary" \
        --trace-processor-binary "$trace_processor_binary" >"$prof_log" 2>&1 &
    active_profiler_pid=$!

    for _ in $(seq 1 60); do
        if grep -q 'Profiling active' "$prof_log"; then
            break
        fi
        if ! kill -0 "$active_profiler_pid" 2>/dev/null; then
            printf 'RESULT|%s|OUT_DIR=%s|LAN_STARTUP_MS=MISSING|HEAP_PROFILE_RC=1|HEAP_DUMP_COUNT=0|ALLOC=profiler_failed|HEALTH_SUM=unknown|LOG_LINE=\n' \
                "$label" "$out_dir"
            cat "$prof_log"
            active_profiler_pid=""
            return 0
        fi
        sleep 0.5
    done

    if ! grep -q 'Profiling active' "$prof_log"; then
        printf 'RESULT|%s|OUT_DIR=%s|LAN_STARTUP_MS=MISSING|HEAP_PROFILE_RC=1|HEAP_DUMP_COUNT=0|ALLOC=profiler_active_timeout|HEALTH_SUM=unknown|LOG_LINE=\n' \
            "$label" "$out_dir"
        kill -INT "$active_profiler_pid" 2>/dev/null || true
        active_profiler_pid=""
        return 0
    fi

    measure_lan_startup "$label" || true

    set +e
    wait "$active_profiler_pid"
    local rc=$?
    set -e
    active_profiler_pid=""

    heap_count=$(find "$out_dir" -maxdepth 1 -type f -name 'heap_dump.*.pb.gz' | wc -l)
    if [ -f "$raw_trace" ]; then
        alloc_line=$(query_trace_value "$alloc_sql" "$raw_trace")
        health_sum=$(query_trace_value "$health_sql" "$raw_trace")
    fi

    printf 'RESULT|%s|OUT_DIR=%s|LAN_STARTUP_MS=%s|HEAP_PROFILE_RC=%s|HEAP_DUMP_COUNT=%s|ALLOC=%s|HEALTH_SUM=%s|LOG_LINE=%s\n' \
        "$label" "$out_dir" "$lan_elapsed_ms" "$rc" "$heap_count" \
        "$alloc_line" "$health_sum" "$lan_line"
    find "$out_dir" -maxdepth 1 -type f -printf 'FILE|%f|%s\n' | sort
}

print_config
run_baseline
for interval in "${intervals[@]}"; do
    run_profile_case "$interval"
done
