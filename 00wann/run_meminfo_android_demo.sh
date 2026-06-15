#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
source "$script_dir/common_tools.sh"
unset MSYS_NO_PATHCONV
demo_dir="$script_dir/meminfo_android_demo"
package_name=com.example.meminfodemo
activity="$package_name/.MainActivity"
timestamp=$(date +%Y%m%d_%H%M%S)
out_dir="$script_dir/PerfData/meminfo_demo_$timestamp"

log() {
  printf '[meminfo-demo] %s\n' "$*"
}

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

adb_shell() {
  (export MSYS_NO_PATHCONV=1; run adb shell "$@")
}

wait_for_pid() {
  local deadline=$((SECONDS + 20))
  local pid=""
  while (( SECONDS < deadline )); do
    pid=$(adb shell "pidof $package_name || true" | tr -d '\r' | awk '{print $1}')
    if [[ -n "$pid" ]]; then
      printf '%s\n' "$pid"
      return 0
    fi
    sleep 0.5
  done
  echo "等待 demo 进程超时: $package_name" >&2
  return 1
}

wait_for_ready_log() {
  local deadline=$((SECONDS + 45))
  while (( SECONDS < deadline )); do
    if adb logcat -d -s MeminfoDemo:I MeminfoDemoJni:I '*:S' | grep -q 'MEMINFO_DEMO_READY'; then
      return 0
    fi
    sleep 1
  done
  echo "等待 MEMINFO_DEMO_READY 日志超时" >&2
  adb logcat -d -s MeminfoDemo:I MeminfoDemoJni:I '*:S' >&2 || true
  return 1
}

mkdir -p "$out_dir"

log "构建 APK"
run "$demo_dir/build_demo_apk.sh"
apk="$demo_dir/build/meminfo-demo.apk"

log "安装 APK"
if ! adb install -r "$apk"; then
  log "安装失败，尝试卸载旧签名包后重装"
  adb uninstall "$package_name" || true
  run adb install -r "$apk"
fi

log "采集 baseline"
adb_shell am force-stop "$package_name"
run adb logcat -c
adb_shell am start -n "$activity"
baseline_pid=$(wait_for_pid)
sleep 3
run adb shell dumpsys meminfo "$package_name" >"$out_dir/baseline_meminfo.txt"
log "baseline pid=$baseline_pid 输出: $out_dir/baseline_meminfo.txt"

log "采集 after，启动自动分配"
adb_shell am force-stop "$package_name"
run adb logcat -c
adb_shell am start -n "$activity" --ez auto_allocate true
after_pid=$(wait_for_pid)
wait_for_ready_log
sleep 5
run adb shell dumpsys meminfo "$package_name" >"$out_dir/after_meminfo.txt"
log "after pid=$after_pid 输出: $out_dir/after_meminfo.txt"

log "校验指标增长"
"$(select_python)" "$demo_dir/verify_meminfo_demo.py" \
  --baseline "$out_dir/baseline_meminfo.txt" \
  --after "$out_dir/after_meminfo.txt" | tee "$out_dir/verify.txt"

log "完成，结果目录: $out_dir"
