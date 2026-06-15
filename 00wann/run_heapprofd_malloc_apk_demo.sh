#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"

# shellcheck source=00wann/config.sh
source config.sh
source common_tools.sh
unset MSYS_NO_PATHCONV

sdk_root=$(select_android_sdk_root)
ndk_root=$(select_android_ndk_root)
build_tools=$(select_build_tools_dir "$sdk_root")
android_jar=$(select_android_jar "$sdk_root")
ndk_prebuilt=$(select_ndk_prebuilt_tag "$ndk_root")
clang=${ndk_root}/toolchains/llvm/prebuilt/${ndk_prebuilt}/bin/clang
if [[ -e "${clang}.exe" ]]; then
  clang="${clang}.exe"
fi
trace_processor=$(select_perfetto_tool trace_processor_shell "$PerfettoRoot" "${TRACE_PROCESSOR:-}")
python_bin=$(select_python)
jdk_bin=$(select_unity_jdk_bin || true)
if [[ -n "$jdk_bin" ]]; then
  export PATH="$jdk_bin:$PATH"
fi
aapt=$(find_build_tool "$build_tools" aapt)
zipalign=$(find_build_tool "$build_tools" zipalign)
dx_jar="$build_tools/lib/dx.jar"
apksigner_jar="$build_tools/lib/apksigner.jar"
java_tool=$(command -v java)
javac_tool=$(command -v javac)
keytool_tool=$(command -v keytool)
jar_tool=$(command -v jar)
package_name=com.example.heapprofddemo
activity_name=${package_name}/.MainActivity
out_dir="PerfData/heapprofd_malloc_apk_demo/$(date +%F_%H-%M-%S)"
build_dir="${out_dir}/build"
device_trace="/data/misc/perfetto-traces/heapprofd-malloc-apk-demo-$(date +%s%N)"
total_bytes=${TOTAL_BYTES:-1073741824}
start_delay_seconds=${START_DELAY_SECONDS:-10}
alloc_seconds=${ALLOC_SECONDS:-60}
hold_seconds=${HOLD_SECONDS:-20}
duration_ms=${DURATION_MS:-$(( (start_delay_seconds + alloc_seconds + 5) * 1000 ))}
sampling_interval=${MALLOC_SAMPLING_INTERVAL_BYTES:-4096}
shmem_size=${MALLOC_SHMEM_SIZE_BYTES:-268435456}
keystore=${out_dir}/debug.keystore

mkdir -p "$build_dir/classes" "$build_dir/apk/lib/arm64-v8a"

printf "编译 APK demo native lib\n"
run_host_tool "$clang" --target=aarch64-linux-android23 -O2 -Wall -Wextra -shared -fPIC \
  -o "$build_dir/apk/lib/arm64-v8a/libheapprofddemo.so" \
  heapprofd_malloc_apk_demo/jni/heapprofd_malloc_apk_demo.c \
  -llog

printf "编译 APK demo Java\n"
run_host_tool "$javac_tool" -encoding UTF-8 -source 1.7 -target 1.7 -bootclasspath "$android_jar" \
  -d "$build_dir/classes" \
  heapprofd_malloc_apk_demo/src/com/example/heapprofddemo/MainActivity.java
run_host_tool "$java_tool" -cp "$dx_jar" com.android.dx.command.Main \
  --dex --output="$build_dir/apk/classes.dex" "$build_dir/classes"

printf "打包 APK\n"
run_host_tool "$aapt" package -f \
  -M heapprofd_malloc_apk_demo/AndroidManifest.xml \
  -I "$android_jar" \
  -F "$build_dir/base.apk"
cp "$build_dir/base.apk" "$build_dir/apk/unsigned.apk"
(cd "$build_dir/apk" && run_host_tool "$jar_tool" uf unsigned.apk classes.dex lib/arm64-v8a/libheapprofddemo.so)

run_host_tool "$keytool_tool" -genkeypair -keystore "$keystore" -storepass android -keypass android \
  -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=Android Debug,O=Android,C=US" >/dev/null
run_host_tool "$zipalign" -f 4 "$build_dir/apk/unsigned.apk" "$build_dir/aligned.apk"
JAVA_TOOL_OPTIONS="--add-opens=java.base/java.io=ALL-UNNAMED --add-exports=java.base/sun.security.x509=ALL-UNNAMED --add-exports=java.base/sun.security.pkcs=ALL-UNNAMED" \
  run_host_tool "$java_tool" -jar "$apksigner_jar" sign --ks "$keystore" --ks-pass pass:android \
  --key-pass pass:android --out "$out_dir/heapprofd_malloc_apk_demo.apk" \
  "$build_dir/aligned.apk"

printf "安装 APK: %s\n" "$package_name"
adb uninstall "$package_name" >/dev/null 2>&1 || true
adb install -r "$out_dir/heapprofd_malloc_apk_demo.apk" >/dev/null
export MSYS_NO_PATHCONV=1
adb shell am force-stop "$package_name" >/dev/null
adb shell run-as "$package_name" rm -f files/malloc_demo_result.txt >/dev/null

config_path="$out_dir/perfetto_config.pbtxt"
cat >"$config_path" <<EOF
buffers {
  size_kb: 262144
  fill_policy: RING_BUFFER
}

data_sources {
  config {
    name: "linux.process_stats"
    process_stats_config {
      scan_all_processes_on_start: true
    }
  }
}

data_sources {
  config {
    name: "android.heapprofd"
    heapprofd_config {
      shmem_size_bytes: ${shmem_size}
      sampling_interval_bytes: ${sampling_interval}
      process_cmdline: "${package_name}"
      heaps: "libc.malloc"
      continuous_dump_config {
        dump_interval_ms: 5000
      }
    }
  }
}

duration_ms: ${duration_ms}
write_into_file: true
flush_period_ms: 1000
flush_timeout_ms: 30000
EOF

printf "启动 Perfetto/heapprofd: shmem=%s interval=%s duration_ms=%s\n" \
  "$shmem_size" "$sampling_interval" "$duration_ms"
adb shell "rm -f '$device_trace'"
adb shell perfetto --txt -c - -o "$device_trace" -d <"$config_path"
sleep 1

printf "冷启动 APK demo: total=%s start_delay=%s alloc_seconds=%s hold_seconds=%s\n" \
  "$total_bytes" "$start_delay_seconds" "$alloc_seconds" "$hold_seconds"
adb shell am start -n "$activity_name" \
  --el total_bytes "$total_bytes" \
  --ei start_delay_seconds "$start_delay_seconds" \
  --ei alloc_seconds "$alloc_seconds" \
  --ei hold_seconds "$hold_seconds" >/dev/null

pid=""
for _ in $(seq 1 80); do
  pid=$(adb shell pidof "$package_name" 2>/dev/null | tr -d '\r' || true)
  if [ -n "$pid" ]; then
    break
  fi
  sleep 0.25
done
if [ -z "$pid" ]; then
  printf "ERROR: APK demo 未启动\n" >&2
  exit 1
fi
printf "APK demo pid=%s\n" "$pid"

result_path="$out_dir/malloc_demo_result.txt"
for _ in $(seq 1 $((alloc_seconds * 4 + 80))); do
  if adb shell run-as "$package_name" cat files/malloc_demo_result.txt >"$result_path" 2>/dev/null; then
    if grep -q '^state=allocated' "$result_path"; then
      break
    fi
  fi
  sleep 0.25
done
if ! grep -q '^state=allocated' "$result_path"; then
  printf "ERROR: APK demo 未进入 allocated 状态\n" >&2
  cat "$result_path" >&2 || true
  exit 1
fi

expected_live=$(sed -n 's/^expected_live_bytes=//p' "$result_path")
mallinfo_uordblks=$(sed -n 's/^mallinfo_uordblks=//p' "$result_path")
allocation_count=$(sed -n 's/^allocation_count=//p' "$result_path")
printf "APK demo allocated: allocations=%s expected=%s mallinfo=%s\n" \
  "$allocation_count" "$expected_live" "$mallinfo_uordblks"

meminfo_path="$out_dir/dumpsys_meminfo.txt"
adb shell dumpsys meminfo "$pid" >"$meminfo_path"
printf "dumpsys meminfo 已保存: %s\n" "$meminfo_path"

# 等待 Perfetto 自然结束；默认 duration 在分配完成后 5 秒结束，
# App 的 hold_seconds 需要覆盖这段时间，确保最终 dump 仍看到 live 分配。
sleep 8
adb pull "$device_trace" "$out_dir/malloc_demo.perfetto-trace" >/dev/null
adb shell "rm -f '$device_trace'" >/dev/null

sql_path="$out_dir/query.sql"
cat >"$sql_path" <<EOF
WITH target_process AS (
  SELECT upid
  FROM process
  WHERE pid = ${pid}
  ORDER BY start_ts DESC
  LIMIT 1
),
latest_dump AS (
  SELECT max(ts) AS ts
  FROM heap_profile_allocation h
  JOIN target_process t ON h.upid = t.upid
  WHERE h.heap_name IN ('libc.malloc', 'malloc')
),
malloc_rows AS (
  SELECT
    h.heap_name AS heap_name,
    sum(h.size) AS live_bytes,
    sum(CASE WHEN h.size > 0 THEN h.size ELSE 0 END) AS allocated_bytes,
    sum(CASE WHEN h.size < 0 THEN -h.size ELSE 0 END) AS freed_bytes
  FROM heap_profile_allocation h
  JOIN target_process t ON h.upid = t.upid
  WHERE h.heap_name IN ('libc.malloc', 'malloc')
  GROUP BY h.heap_name
),
latest_malloc_rows AS (
  SELECT
    h.heap_name AS heap_name,
    sum(h.size) AS live_bytes,
    sum(CASE WHEN h.size > 0 THEN h.size ELSE 0 END) AS allocated_bytes,
    sum(CASE WHEN h.size < 0 THEN -h.size ELSE 0 END) AS freed_bytes
  FROM heap_profile_allocation h
  JOIN target_process t ON h.upid = t.upid
  JOIN latest_dump d ON h.ts = d.ts
  WHERE h.heap_name IN ('libc.malloc', 'malloc')
  GROUP BY h.heap_name
)
SELECT 'malloc_cumulative' AS section, heap_name AS c0, live_bytes AS c1,
       allocated_bytes AS c2, freed_bytes AS c3
FROM malloc_rows
UNION ALL
SELECT 'malloc_latest' AS section, heap_name AS c0, live_bytes AS c1,
       allocated_bytes AS c2, freed_bytes AS c3
FROM latest_malloc_rows
UNION ALL
SELECT 'stat' AS section, name AS c0, idx AS c1, value AS c2, '' AS c3
FROM stats
WHERE name IN (
  'heapprofd_buffer_overran',
  'heapprofd_client_error',
  'heapprofd_missing_packet',
  'heapprofd_non_finalized_profile',
  'traced_buf_bytes_written',
  'traced_buf_buffer_size'
)
ORDER BY section, c0;
EOF

query_csv="$out_dir/trace_processor.csv"
"$trace_processor" query "$out_dir/malloc_demo.perfetto-trace" "$(cat "$sql_path")" >"$query_csv"

"$python_bin" - "$out_dir" "$expected_live" "$mallinfo_uordblks" "$allocation_count" "$total_bytes" "$meminfo_path" "$query_csv" <<'PY'
import csv
import json
import re
import sys

out_dir, expected_live, mallinfo_uordblks, allocation_count, total_bytes, meminfo_path, query_csv = sys.argv[1:]
expected_live = int(expected_live)
mallinfo_uordblks = int(mallinfo_uordblks)
allocation_count = int(allocation_count)
total_bytes = int(total_bytes)

def numbers(line):
    return [int(item.replace(",", "")) for item in re.findall(r"-?[\d,]+", line)]

meminfo = {
    "native_heap_pss_bytes": 0,
    "native_heap_swap_pss_bytes": 0,
    "native_heap_pss_plus_swap_pss_bytes": 0,
    "native_heap_alloc_bytes": 0,
    "total_pss_bytes": 0,
}
with open(meminfo_path, "r", encoding="utf-8") as fd:
    for raw in fd:
        line = raw.strip()
        vals = numbers(line)
        if line.startswith("Native Heap") and vals:
            meminfo["native_heap_pss_bytes"] = vals[0] * 1024
            if len(vals) >= 4:
                meminfo["native_heap_swap_pss_bytes"] = vals[3] * 1024
            if len(vals) >= 3:
                meminfo["native_heap_alloc_bytes"] = vals[-2] * 1024
        elif line.startswith("TOTAL PSS:") and vals:
            meminfo["total_pss_bytes"] = vals[0] * 1024
        elif re.match(r"^TOTAL\s+-?[\d,]+", line) and vals:
            meminfo["total_pss_bytes"] = vals[0] * 1024
meminfo["native_heap_pss_plus_swap_pss_bytes"] = (
    meminfo["native_heap_pss_bytes"] + meminfo["native_heap_swap_pss_bytes"]
)

malloc = {"live_bytes": 0, "allocated_bytes": 0, "freed_bytes": 0, "heaps": []}
malloc_latest = {"live_bytes": 0, "allocated_bytes": 0, "freed_bytes": 0, "heaps": []}
stats = {}
with open(query_csv, "r", encoding="utf-8") as fd:
    reader = csv.DictReader(fd)
    for row in reader:
        section = row.get("section", "").strip('"')
        if section in ("malloc_cumulative", "malloc_latest"):
            heap = {
                "heap_name": row.get("c0", "").strip('"'),
                "live_bytes": int(row.get("c1") or 0),
                "allocated_bytes": int(row.get("c2") or 0),
                "freed_bytes": int(row.get("c3") or 0),
            }
            target = malloc if section == "malloc_cumulative" else malloc_latest
            target["heaps"].append(heap)
            target["live_bytes"] += heap["live_bytes"]
            target["allocated_bytes"] += heap["allocated_bytes"]
            target["freed_bytes"] += heap["freed_bytes"]
        elif section == "stat":
            stats[row.get("c0", "").strip('"')] = int(row.get("c2") or 0)

def ratio(value, ref):
    return None if ref == 0 else abs(value - ref) / ref

health = {
    "heapprofd_data_loss": (
        stats.get("heapprofd_buffer_overran", 0)
        + stats.get("heapprofd_missing_packet", 0)
        + stats.get("heapprofd_non_finalized_profile", 0)
    ),
    "heapprofd_errors": stats.get("heapprofd_client_error", 0),
    "raw_stats": stats,
}
report = {
    "units": "bytes",
    "note": (
        "主判断使用 heapprofd.cumulative.live_bytes；continuous dump 下 "
        "heapprofd.latest_dump.live_bytes 只表示最后一个 dump 分片诊断口径。"
    ),
    "demo": {
        "requested_total_bytes": total_bytes,
        "allocation_count": allocation_count,
        "expected_live_bytes": expected_live,
        "mallinfo_uordblks": mallinfo_uordblks,
    },
    "heapprofd": {
        "cumulative": malloc,
        "latest_dump": malloc_latest,
    },
    "meminfo": meminfo,
    "health": health,
    "comparison": {
        "heapprofd_cumulative_live_vs_expected_ratio": ratio(malloc["live_bytes"], expected_live),
        "heapprofd_cumulative_live_vs_mallinfo_ratio": ratio(malloc["live_bytes"], mallinfo_uordblks),
        "heapprofd_cumulative_live_vs_meminfo_native_heap_alloc_ratio":
            ratio(malloc["live_bytes"], meminfo["native_heap_alloc_bytes"]),
        "heapprofd_cumulative_live_vs_meminfo_native_heap_pss_ratio":
            ratio(malloc["live_bytes"], meminfo["native_heap_pss_bytes"]),
        "heapprofd_cumulative_live_vs_meminfo_native_heap_pss_plus_swap_pss_ratio":
            ratio(malloc["live_bytes"], meminfo["native_heap_pss_plus_swap_pss_bytes"]),
        "heapprofd_latest_dump_live_vs_expected_ratio":
            ratio(malloc_latest["live_bytes"], expected_live),
        "meminfo_native_heap_alloc_vs_expected_ratio":
            ratio(meminfo["native_heap_alloc_bytes"], expected_live),
        "meminfo_native_heap_pss_vs_expected_ratio":
            ratio(meminfo["native_heap_pss_bytes"], expected_live),
        "meminfo_native_heap_pss_plus_swap_pss_vs_expected_ratio":
            ratio(meminfo["native_heap_pss_plus_swap_pss_bytes"], expected_live),
    },
}
path = f"{out_dir}/malloc_demo_report.json"
with open(path, "w", encoding="utf-8") as fd:
    json.dump(report, fd, ensure_ascii=False, indent=2)
    fd.write("\n")
print("APK demo 验证报告:")
print(f"  allocations: {allocation_count}")
print(f"  expected live: {expected_live}")
print(f"  mallinfo uordblks: {mallinfo_uordblks}")
print(f"  heapprofd cumulative live: {malloc['live_bytes']}")
print(f"  heapprofd latest dump live (诊断分片): {malloc_latest['live_bytes']}")
print(f"  meminfo Native Heap Alloc: {meminfo['native_heap_alloc_bytes']}")
print(f"  meminfo Native Heap PSS: {meminfo['native_heap_pss_bytes']}")
print(f"  meminfo Native Heap SwapPss: {meminfo['native_heap_swap_pss_bytes']}")
print(f"  meminfo Native Heap PSS+SwapPss: {meminfo['native_heap_pss_plus_swap_pss_bytes']}")
print(f"  heapprofd data_loss: {health['heapprofd_data_loss']}")
print(f"  heapprofd errors: {health['heapprofd_errors']}")
print(f"  report: {path}")
PY
