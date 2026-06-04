#!/usr/bin/env bash
set -euo pipefail

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

script_dir=$(cd "$(dirname "$0")" && pwd)
if [[ ! -f "$script_dir/run_heap_profile.py" ]]; then
  echo "缺少 Python 实现: run_heap_profile.py"
  exit 1
fi
cp "$script_dir/run_heap_profile.sh" "$tmpdir/run_heap_profile.sh"
cp "$script_dir/run_heap_profile.py" "$tmpdir/run_heap_profile.py"
cp "$script_dir/debugconfig.txt" "$tmpdir/debugconfig.txt"

if [[ "$(head -n 1 "$tmpdir/run_heap_profile.sh")" != "#!/usr/bin/env bash" ]]; then
  echo "run_heap_profile.sh 缺少 bash shebang，直接执行时可能被 /bin/sh 解释"
  exit 1
fi
if ! grep -Fq "run_heap_profile.py" "$tmpdir/run_heap_profile.sh"; then
  echo "run_heap_profile.sh 应只作为 Python 实现入口包装"
  exit 1
fi

# 构造最小运行环境，避免测试依赖真实 Perfetto 输出目录和 Android 设备。
cat >"$tmpdir/config.sh" <<EOF
PerfettoRoot="$tmpdir/perfetto"
EOF
cat >"$tmpdir/fsbootcmd_push_to_phone.sh" <<'EOF'
:
EOF

mkdir -p "$tmpdir/bin" "$tmpdir/perfetto/buildtools/linux64/clang/bin" "$tmpdir/perfetto/out/linux_clang_release"
# fake adb 第一次 pidof 返回空，第二次返回 PID，用于验证脚本会先拉起应用再继续采集。
cat >"$tmpdir/bin/adb" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
log_file="${TEST_LOG:?}"
pidof_count_file="${PIDOF_COUNT_FILE:?}"
printf 'adb %s\n' "$*" >>"$log_file"
if [[ "$1" == "shell" && "$2" == "pidof" ]]; then
  count=$(cat "$pidof_count_file")
  count=$((count + 1))
  printf '%s' "$count" >"$pidof_count_file"
  if [[ "$count" -ge 2 ]]; then
    printf '4321\n'
  fi
elif [[ "$1" == "shell" && "$2" == "dumpsys" && "$3" == "meminfo" ]]; then
  native_heap_alloc_kb="${FAKE_NATIVE_HEAP_ALLOC_KB:-102400}"
  cat <<MEMINFO
Applications Memory Usage (in Kilobytes):
** MEMINFO in pid 4321 [com.tencent.dhwdxkty.trunk.profiler] **
                   Pss  Private  Private     Swap      Rss     Heap     Heap     Heap
                 Total    Dirty    Clean    Dirty    Total     Size    Alloc     Free
                ------   ------   ------   ------   ------   ------   ------   ------
  Native Heap   101000   100000        0        0   102000   120000   ${native_heap_alloc_kb}    17600
  Dalvik Heap     1000      900        0        0     2000     3000     1000     2000
        TOTAL   102000   100900        0        0   104000   123000   103400    19600
MEMINFO
fi
EOF
cat >"$tmpdir/bin/cp" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'cp %s\n' "$*" >>"${TEST_LOG:?}"
EOF
cat >"$tmpdir/bin/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'python3 %s\n' "$*" >>"${TEST_LOG:?}"
printf 'PYTHONPATH=%s\n' "${PYTHONPATH:-}" >>"${TEST_LOG:?}"
printf 'PYTHONUNBUFFERED=%s\n' "${PYTHONUNBUFFERED:-}" >>"${TEST_LOG:?}"
printf 'Profiling active. Press Ctrl+C to terminate.\n'
out_dir=""
while [[ "$#" -gt 0 ]]; do
  if [[ "$1" == "-o" ]]; then
    out_dir="$2"
    break
  fi
  shift
done

write_fake_profile_outputs() {
  if [[ "$out_dir" == "" ]]; then
    return 0
  fi
  mkdir -p "$out_dir"
  printf 'fake trace\n' >"$out_dir/symbolized-trace"
  printf 'fake raw trace\n' >"$out_dir/raw-trace"
  printf 'fake heap dump\n' >"$out_dir/heap_dump.1.4321.libc.malloc.pb.gz"
}

if [[ "${FAKE_HEAP_PROFILE_WAIT_FOR_SIGINT:-0}" == "1" ]]; then
  # 模拟 Perfetto heap_profile.py：人工 Ctrl+C 后先进入 profiler shutdown，再做 host 后处理。
  trap 'printf "PYTHON_GOT_SIGINT\n" >>"${TEST_LOG:?}"; printf "Waiting for profiler shutdown...\n"; write_fake_profile_outputs; sleep 0.5; printf "PYTHON_SIGINT_DONE\n" >>"${TEST_LOG:?}"; exit 0' INT
  while true; do
    sleep 0.2
  done
fi

printf 'Waiting for profiler shutdown...\n'
write_fake_profile_outputs
exit "${FAKE_HEAP_PROFILE_RC:-0}"
EOF
chmod +x "$tmpdir/bin/adb" "$tmpdir/bin/cp" "$tmpdir/bin/python3"
cat >"$tmpdir/perfetto/out/linux_clang_release/trace_processor_shell" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'trace_processor_shell %s\n' "$*" >>"${TEST_LOG:?}"
query=""
while [[ "$#" -gt 0 ]]; do
  if [[ "$1" == "-Q" ]]; then
    query="$2"
    shift 2
    continue
  fi
  shift
done
if [[ "$query" == *"sum(size)"* ]]; then
  printf '"malloc_live_bytes"\n'
  printf '%s\n' "${FAKE_MALLOC_LIVE_BYTES:-104857600}"
elif [[ "$query" == *"stats"* ]]; then
  printf '"health_sum"\n'
  printf '0\n'
else
  printf '"value"\n'
  printf '0\n'
fi
EOF
chmod +x "$tmpdir/perfetto/out/linux_clang_release/trace_processor_shell"

export PATH="$tmpdir/bin:$PATH"
export TEST_LOG="$tmpdir/commands.log"
export PIDOF_COUNT_FILE="$tmpdir/pidof_count"

run_script() {
  local log_file=$1
  shift
  : >"$log_file"
  printf '0' >"$PIDOF_COUNT_FILE"
  cd /
  "$tmpdir/run_heap_profile.sh" "$@" >"$tmpdir/run_heap_profile_test.out"
}

expected_app="com.tencent.dhwdxkty.trunk.profiler"
run_script "$TEST_LOG"
if ! grep -Fq "adb shell monkey -p $expected_app 1" "$TEST_LOG"; then
  echo "未在目标进程缺失时启动应用"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "adb shell am force-stop $expected_app" "$TEST_LOG"; then
  echo "采集前未重启目标应用，malloc live 总量无法和 meminfo Native Heap Alloc 对齐"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "python3 $tmpdir/perfetto/python/tools/heap_profile.py -n $expected_app -o" "$TEST_LOG"; then
  echo "未继续执行 heap profile"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--no-running" "$TEST_LOG"; then
  echo "未在目标进程启动前启用 heapprofd"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--traceconv-binary $tmpdir/perfetto/out/linux_clang_release/traceconv" "$TEST_LOG"; then
  echo "未显式使用本地构建的 traceconv"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--trace-processor-binary $tmpdir/perfetto/out/linux_clang_release/trace_processor_shell" "$TEST_LOG"; then
  echo "未显式使用本地构建的 trace_processor_shell"
  cat "$TEST_LOG"
  exit 1
fi
if grep -Fq -- "-d " "$TEST_LOG"; then
  echo "默认采集不应设置时长限制"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "-i 1024" "$TEST_LOG"; then
  echo "默认采集应设置采样 interval 为 1024"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--shmem-size 8388608" "$TEST_LOG"; then
  echo "默认采集应设置 heapprofd 共享缓冲区为 8M"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "PYTHONPATH=$tmpdir/perfetto/python" "$TEST_LOG"; then
  echo "未把 Perfetto Python 包目录传给 heap_profile.py"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "PYTHONUNBUFFERED=1" "$TEST_LOG"; then
  echo "未禁用 Python stdout 缓冲，可能导致等待 Profiling active 后才启动应用"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "adb shell dumpsys meminfo $expected_app" "$TEST_LOG"; then
  echo "采集后未立即抓取目标应用 dumpsys meminfo"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "trace_processor_shell -Q select coalesce(sum(size), 0) as malloc_live_bytes from heap_profile_allocation;" "$TEST_LOG"; then
  echo "未用 heap_profile_allocation 全窗口 sum(size) 计算 malloc live"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "malloc_live_bytes=104857600" "$tmpdir/run_heap_profile_test.out"; then
  echo "未输出 trace malloc live 总量"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "meminfo_native_heap_alloc_bytes=104857600" "$tmpdir/run_heap_profile_test.out"; then
  echo "未输出 meminfo Native Heap Alloc 总量"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "HEAP_MEMINFO_VALIDATION=PASS" "$tmpdir/run_heap_profile_test.out"; then
  echo "malloc live 与 meminfo Native Heap Alloc 相当时应通过验证"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
meminfo_line=$(grep -n "adb shell dumpsys meminfo $expected_app" "$TEST_LOG" | head -1 | cut -d: -f1)
profile_log_line=$(grep -n "cp /tmp/run_heap_profile_.*heap_profile.log" "$TEST_LOG" | head -1 | cut -d: -f1)
if [[ "$meminfo_line" == "" || "$profile_log_line" == "" || "$meminfo_line" -ge "$profile_log_line" ]]; then
  echo "应在 profiler 进入 shutdown 后、host 后处理完成前抓取 meminfo"
  cat "$TEST_LOG"
  exit 1
fi

run_script "$TEST_LOG" 45000
if ! grep -Fq -- "-d 45000" "$TEST_LOG"; then
  echo "传入 45000 时未设置 45 秒自动退出采集时长"
  cat "$TEST_LOG"
  exit 1
fi

run_script "$TEST_LOG" 45000 1024
if ! grep -Fq -- "-d 45000" "$TEST_LOG"; then
  echo "传入 45000 时未设置 45 秒自动退出采集时长"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "-i 1024" "$TEST_LOG"; then
  echo "传入 1024 时未设置采样 interval"
  cat "$TEST_LOG"
  exit 1
fi

run_script "$TEST_LOG" 45000 1024 67108864
if ! grep -Fq -- "--shmem-size 67108864" "$TEST_LOG"; then
  echo "传入 67108864 时未设置 heapprofd 共享缓冲区大小"
  cat "$TEST_LOG"
  exit 1
fi

export FAKE_HEAP_PROFILE_WAIT_FOR_SIGINT=1
: >"$TEST_LOG"
printf '0' >"$PIDOF_COUNT_FILE"
cd /
set +e
/usr/bin/python3 - "$tmpdir/run_heap_profile.sh" "$TEST_LOG" "$tmpdir/run_heap_profile_sigint.out" <<'PY'
import os
import signal
import subprocess
import sys
import time

script, test_log, output_path = sys.argv[1:4]

def restore_sigint():
  signal.signal(signal.SIGINT, signal.SIG_DFL)

with open(output_path, "w", encoding="utf-8") as output:
  proc = subprocess.Popen(
      [script],
      stdout=output,
      stderr=subprocess.STDOUT,
      start_new_session=True,
      preexec_fn=restore_sigint)

  deadline = time.time() + 5
  while time.time() < deadline:
    try:
      with open(test_log, "r", encoding="utf-8") as log:
        if "adb shell monkey -p com.tencent.dhwdxkty.trunk.profiler 1" in log.read():
          break
    except FileNotFoundError:
      pass
    time.sleep(0.1)
  else:
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(timeout=5)
    sys.exit(125)

  os.kill(proc.pid, signal.SIGINT)
  try:
    rc = proc.wait(timeout=8)
  except subprocess.TimeoutExpired:
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(timeout=5)
    sys.exit(124)
sys.exit(rc if rc >= 0 else 128 - rc)
PY
sigint_rc=$?
set -e
unset FAKE_HEAP_PROFILE_WAIT_FOR_SIGINT
if [[ "$sigint_rc" -ne 0 ]]; then
  echo "人工 Ctrl+C 后应等待 heap_profile.py 收尾并继续执行验证，不应直接 130 退出"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_sigint.out"
  exit 1
fi
if ! grep -Fq "PYTHON_GOT_SIGINT" "$TEST_LOG"; then
  echo "人工 Ctrl+C 未转发给 heap_profile.py"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "PYTHON_SIGINT_DONE" "$TEST_LOG"; then
  echo "脚本未等待 heap_profile.py 完成 Ctrl+C 后的 trace 收尾"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "HEAP_MEMINFO_VALIDATION=PASS" "$tmpdir/run_heap_profile_sigint.out"; then
  echo "人工 Ctrl+C 收尾后未继续执行 malloc live 与 meminfo 验证"
  cat "$tmpdir/run_heap_profile_sigint.out"
  exit 1
fi

export FAKE_HEAP_PROFILE_RC=1
set +e
run_script "$TEST_LOG" 45000
failed_rc=$?
set -e
unset FAKE_HEAP_PROFILE_RC
if [[ "$failed_rc" -eq 0 ]]; then
  echo "heap_profile.py 失败时脚本不应误报成功"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "adb shell dumpsys meminfo $expected_app" "$TEST_LOG"; then
  echo "heap_profile.py 失败但已有 trace 时仍应抓取 meminfo 诊断根因"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "trace_processor_shell -Q select coalesce(sum(size), 0) as malloc_live_bytes from heap_profile_allocation;" "$TEST_LOG"; then
  echo "heap_profile.py 失败但已有 trace 时仍应查询 malloc live"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "HEAP_PROFILE_FAILED|rc=1" "$tmpdir/run_heap_profile_test.out"; then
  echo "heap_profile.py 失败时应输出原始失败码"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi

export FAKE_MALLOC_LIVE_BYTES=696873169
export FAKE_NATIVE_HEAP_ALLOC_KB=913859
set +e
run_script "$TEST_LOG" 45000
large_diff_rc=$?
set -e
unset FAKE_MALLOC_LIVE_BYTES FAKE_NATIVE_HEAP_ALLOC_KB
if [[ "$large_diff_rc" -eq 0 ]]; then
  echo "malloc live 与 meminfo Native Heap Alloc 相差约 227MiB 时不应通过验证"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "HEAP_MEMINFO_VALIDATION=FAIL" "$tmpdir/run_heap_profile_test.out"; then
  echo "大差异失败时应输出 HEAP_MEMINFO_VALIDATION=FAIL"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
