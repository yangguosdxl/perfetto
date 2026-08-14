#!/usr/bin/env bash
set -euo pipefail

tmpdir=$(mktemp -d)
if [[ "${KEEP_TEST_TMP:-0}" == "1" ]]; then
  echo "保留测试临时目录: $tmpdir"
else
  trap 'rm -rf "$tmpdir"' EXIT
fi

script_dir=$(cd "$(dirname "$0")" && pwd)
if [[ ! -f "$script_dir/run_heap_profile.py" ]]; then
  echo "缺少 Python 实现: run_heap_profile.py"
  exit 1
fi
cp "$script_dir/run_heap_profile.sh" "$tmpdir/run_heap_profile.sh"
cp "$script_dir/run_heap_profile.py" "$tmpdir/run_heap_profile.py"
cp "$script_dir/run_device_test.sh" "$tmpdir/run_device_test.sh"
cp "$script_dir/run_device_test.py" "$tmpdir/run_device_test.py"
cp "$script_dir/device_test.ini" "$tmpdir/device_test.ini"
cp -R "$script_dir/device_test_framework" "$tmpdir/device_test_framework"
cp -R "$script_dir/device_test_plugins" "$tmpdir/device_test_plugins"
cp "$script_dir/profile_action_api.py" "$tmpdir/profile_action_api.py"
cp "$script_dir/profile_action_runner.py" "$tmpdir/profile_action_runner.py"
mkdir -p "$tmpdir/profile_actions"
cp "$script_dir/profile_actions/send_battle_record_gm.py" \
  "$tmpdir/profile_actions/send_battle_record_gm.py"
cp "$script_dir/common_tools.sh" "$tmpdir/common_tools.sh"
cp "$script_dir/debugconfig.txt" "$tmpdir/debugconfig.txt"

if [[ "$(head -n 1 "$tmpdir/run_heap_profile.sh")" != "#!/usr/bin/env bash" ]]; then
  echo "run_heap_profile.sh 缺少 bash shebang，直接执行时可能被 /bin/sh 解释"
  exit 1
fi
if ! grep -Fq "run_device_test.sh" "$tmpdir/run_heap_profile.sh"; then
  echo "run_heap_profile.sh 应只作为统一真机测试入口包装"
  exit 1
fi

# 构造最小运行环境，避免测试依赖真实 Perfetto 输出目录和 Android 设备。
cat >"$tmpdir/config.sh" <<EOF
export PerfettoRoot="$tmpdir/perfetto"
export MMAP_PHYS_APP="com.example.heapprofile"
export ANDROID_SERIAL="FAKE_HEAP_DEVICE"
export PERF_PROFILE_ACTION_SCRIPT="profile_actions/send_battle_record_gm.py"
EOF
cat >"$tmpdir/fsbootcmd_push_to_phone.sh" <<'EOF'
:
EOF

mkdir -p \
  "$tmpdir/bin" \
  "$tmpdir/perfetto/buildtools/linux64/clang/bin" \
  "$tmpdir/perfetto/buildtools/win/clang/bin" \
  "$tmpdir/perfetto/out/linux_clang_release" \
  "$tmpdir/perfetto/out/win_clang" \
  "$tmpdir/perfetto/python/tools"
printf 'fake traceconv\n' >"$tmpdir/perfetto/out/linux_clang_release/traceconv"
# fake adb 第一次 pidof 返回空，第二次返回 PID，用于验证脚本会先拉起应用再继续采集。
cat >"$tmpdir/bin/adb" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
log_file="${TEST_LOG:?}"
pidof_count_file="${PIDOF_COUNT_FILE:?}"
printf 'adb %s\n' "$*" >>"$log_file"
# 公共测试 API 会显式选择设备；去掉前缀后再按 ADB 子命令模拟响应。
if [[ "${1:-}" == "-s" ]]; then
  [[ "${2:-}" == "FAKE_HEAP_DEVICE" ]]
  shift 2
fi
if [[ "$1" == "get-state" ]]; then
  printf 'device\n'
elif [[ "$1" == "shell" && "$2" == "settings" && "$3" == "get" ]]; then
  printf 'null\n'
elif [[ "$1" == "shell" && "$2" == "pidof" ]]; then
  count=$(cat "$pidof_count_file")
  count=$((count + 1))
  printf '%s' "$count" >"$pidof_count_file"
  if [[ "${FAKE_PIDOF_NEVER:-0}" != "1" && "$count" -ge 2 ]]; then
    printf '4321\n'
  fi
elif [[ "$1" == "shell" && "$2" == "am" && "$3" == "start" ]]; then
  exit "${FAKE_AM_START_RC:-0}"
elif [[ "$1" == "logcat" && "$2" == "-c" ]]; then
  :
elif [[ "$1" == "logcat" && "$2" == "-v" && "$3" == "time" ]]; then
  printf 'FAKE_LOGCAT_STARTED\n' >>"$log_file"
  trap 'printf "FAKE_LOGCAT_TERM\n" >>"$log_file"; exit 0' TERM INT
  printf '06-16 15:00:00.000 I/FS( 4321): 启动登录流程\n'
  printf '06-16 15:00:00.500 I/Unity   ( 4321): Tcp server started and listening at 5002\n'
  if [[ "${FAKE_LOGCAT_NO_LOGIN:-0}" != "1" ]]; then
    printf '06-16 15:00:01.000 I/FS( 4321): 登录场景完成\n'
    printf '06-16 15:00:02.000 I/FS( 4321): RegistForGameStart.LoadOtherTable.End\n'
  fi
  if [[ "${FAKE_LOGCAT_LONG_RUNNING:-0}" == "1" ]]; then
    parent_pid=$PPID
    after_login_poll_count=0
    after_login_marker_written=0
    while true; do
      if ! kill -0 "$parent_pid" 2>/dev/null; then
        printf 'FAKE_LOGCAT_PARENT_GONE\n' >>"$log_file"
        exit 0
      fi
      sleep 0.1
      after_login_poll_count=$((after_login_poll_count + 1))
      if [[ "${FAKE_LOGCAT_MARK_AFTER_LOGIN:-0}" == "1" && "$after_login_poll_count" -ge 10 && "$after_login_marker_written" == "0" ]]; then
        printf 'FAKE_LOGCAT_AFTER_LOGIN_DELAY\n' >>"$log_file"
        after_login_marker_written=1
      fi
    done
  fi
  printf 'FAKE_LOGCAT_EXIT\n' >>"$log_file"
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
cat >"$tmpdir/perfetto/python/tools/heap_profile.py" <<'PY'
#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log_path = os.environ["TEST_LOG"]
if os.name == "nt" and log_path.startswith("/"):
  proc = subprocess.run(
      ["cygpath", "-w", log_path],
      text=True,
      stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL,
      check=False)
  if proc.returncode == 0 and proc.stdout.strip():
    log_path = proc.stdout.strip()
with open(log_path, "a", encoding="utf-8") as log:
  log.write("heap_profile.py " + " ".join(sys.argv[1:]) + "\n")
  log.write("PYTHONPATH=" + os.environ.get("PYTHONPATH", "") + "\n")
  log.write("PYTHONUNBUFFERED=" + os.environ.get("PYTHONUNBUFFERED", "") + "\n")
  log.write("PATH=" + os.environ.get("PATH", "") + "\n")

out_dir = ""
for index, arg in enumerate(sys.argv[1:]):
  if arg == "-o" and index + 2 <= len(sys.argv):
    out_dir = sys.argv[index + 2]
    break

def write_fake_profile_outputs():
  if not out_dir:
    return
  output = Path(out_dir)
  output.mkdir(parents=True, exist_ok=True)
  (output / "symbolized-trace").write_text("fake trace\n", encoding="utf-8")
  (output / "raw-trace").write_text("fake raw trace\n", encoding="utf-8")
  (output / "heap_dump.1.4321.libc.malloc.pb").write_text(
      "fake heap dump\n", encoding="utf-8")

print("Profiling active. Press Ctrl+C to terminate.", flush=True)

if os.environ.get("FAKE_HEAP_PROFILE_WAIT_FOR_SIGINT") == "1":
  if os.environ.get("FAKE_HEAP_PROFILE_IGNORE_SIGINT") == "1":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
      time.sleep(0.2)

  def on_sigint(_signum, _frame):
    with open(log_path, "a", encoding="utf-8") as log:
      log.write("PYTHON_GOT_SIGINT\n")
    print("Waiting for profiler shutdown...", flush=True)
    write_fake_profile_outputs()
    time.sleep(0.5)
    with open(log_path, "a", encoding="utf-8") as log:
      log.write("PYTHON_SIGINT_DONE\n")
    raise SystemExit(int(os.environ.get("FAKE_HEAP_PROFILE_RC", "0")))

  signal.signal(signal.SIGINT, on_sigint)
  while True:
    time.sleep(0.2)

print("Waiting for profiler shutdown...", flush=True)
write_fake_profile_outputs()
raise SystemExit(int(os.environ.get("FAKE_HEAP_PROFILE_RC", "0")))
PY
cat >"$tmpdir/fake_rpc_server.py" <<'PY'
#!/usr/bin/env python3
import json
import os
import socket
import struct
from pathlib import Path


def receive_exact(sock, size):
  data = b""
  while len(data) < size:
    chunk = sock.recv(size - len(data))
    if not chunk:
      raise RuntimeError("测试 RPC 请求不完整")
    data += chunk
  return data


port = int(os.environ["HEAP_PROFILE_RPC_LOCAL_PORT"])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
  server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  server.bind(("127.0.0.1", port))
  server.listen(1)
  server.settimeout(15)
  Path(os.environ["FAKE_RPC_READY_HOST_FILE"]).write_text(
      "ready", encoding="utf-8")
  conn, _ = server.accept()
  with conn:
    conn.settimeout(5)
    size = struct.unpack("<I", receive_exact(conn, 4))[0]
    request = json.loads(receive_exact(conn, size).decode("utf-8"))
    with open(os.environ["TEST_LOG_HOST_PATH"], "a", encoding="utf-8") as log:
      log.write(
          "FAKE_RPC_REQUEST|"
          f"method={request.get('method')}|"
          f"params={json.dumps(request.get('params'), ensure_ascii=False)}\n")
    if os.environ.get("FAKE_RPC_ERROR") == "1":
      response = {
          "jsonrpc": "2.0",
          "id": request.get("id"),
          "error": {"code": 1, "message": "测试 RPC 失败"},
      }
    else:
      response = {
          "jsonrpc": "2.0",
          "id": request.get("id"),
          "result": True,
      }
    body = json.dumps(response, ensure_ascii=True).encode("utf-8")
    conn.sendall(struct.pack("<I", len(body)) + body)
PY
chmod +x "$tmpdir/bin/adb" "$tmpdir/bin/cp" "$tmpdir/perfetto/python/tools/heap_profile.py"
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
chmod +x "$tmpdir/perfetto/out/linux_clang_release/trace_processor_shell" "$tmpdir/perfetto/out/linux_clang_release/traceconv"
cp "$tmpdir/perfetto/out/linux_clang_release/trace_processor_shell" "$tmpdir/perfetto/out/win_clang/trace_processor_shell"
cp "$tmpdir/perfetto/out/linux_clang_release/traceconv" "$tmpdir/perfetto/out/win_clang/traceconv"
chmod +x "$tmpdir/perfetto/out/win_clang/trace_processor_shell" "$tmpdir/perfetto/out/win_clang/traceconv"

if command -v cygpath >/dev/null 2>&1; then
  bash_exe=$(cygpath -w "$(command -v bash)")
  for name in adb cp; do
    cat >"$tmpdir/bin/${name}.cmd" <<EOF
@"$bash_exe" "%~dp0${name}" %*
EOF
  done
  for name in trace_processor_shell traceconv; do
    cat >"$tmpdir/perfetto/out/win_clang/${name}.cmd" <<EOF
@"$bash_exe" "%~dp0${name}" %*
EOF
  done
fi

real_python=$(command -v python3 || command -v python || command -v py)
export RUN_HEAP_PROFILE_PYTHON="$real_python"
export RUN_HEAP_PROFILE_INNER_PYTHON="$real_python"
export DEVICE_TEST_PYTHON="$real_python"
export DEVICE_TEST_BACKEND_PYTHON="$real_python"
export RUN_HEAP_PROFILE_EXTRA_PATH="$tmpdir/bin"
export ADB_BINARY="$tmpdir/bin/adb"
export CP_BINARY="$tmpdir/bin/cp"
if command -v cygpath >/dev/null 2>&1; then
  export DEVICE_TEST_BACKEND_PYTHON="$(cygpath -w "$real_python")"
  export RUN_HEAP_PROFILE_EXTRA_PATH="$(cygpath -w "$tmpdir/bin")"
  export ADB_BINARY="$(cygpath -w "$tmpdir/bin/adb.cmd")"
  export CP_BINARY="$(cygpath -w "$tmpdir/bin/cp.cmd")"
fi
export PATH="$tmpdir/bin:$PATH"
export FAKE_LOGCAT_LONG_RUNNING=1
export FAKE_LOGCAT_MARK_AFTER_LOGIN=1
export HEAP_PROFILE_LOGIN_STABLE_S=1
export HEAP_PROFILE_RPC_LOCAL_PORT=22346
export TEST_LOG="$tmpdir/commands.log"
export PIDOF_COUNT_FILE="$tmpdir/pidof_count"
export FAKE_RPC_READY_FILE="$tmpdir/fake_rpc.ready"
python_run_heap_profile_path="$tmpdir/run_heap_profile.py"
python_tmpdir_path="$tmpdir"
python_perfetto_path="$tmpdir/perfetto"
fake_rpc_server_path="$tmpdir/fake_rpc_server.py"
python_action_path="$tmpdir/profile_actions/send_battle_record_gm.py"
fake_rpc_ready_host_file="$FAKE_RPC_READY_FILE"
test_log_host_path="$TEST_LOG"
if command -v cygpath >/dev/null 2>&1; then
  python_run_heap_profile_path=$(cygpath -w "$python_run_heap_profile_path")
  python_tmpdir_path=$(cygpath -w "$python_tmpdir_path")
  python_perfetto_path=$(cygpath -w "$python_perfetto_path")
  fake_rpc_server_path=$(cygpath -w "$fake_rpc_server_path")
  python_action_path=$(cygpath -w "$python_action_path")
  fake_rpc_ready_host_file=$(cygpath -w "$fake_rpc_ready_host_file")
  test_log_host_path=$(cygpath -w "$test_log_host_path")
fi
export FAKE_RPC_READY_HOST_FILE="$fake_rpc_ready_host_file"
export TEST_LOG_HOST_PATH="$test_log_host_path"

mkdir -p "$tmpdir/fs_symbols/arm64-v8a" "$tmpdir/workspace/allsymbols/arm64-v8a"
env -u PERFETTO_BINARY_PATH \
  RUN_HEAP_PROFILE_SYMBOLS_DIR="$tmpdir/fs_symbols/arm64-v8a" \
  "$real_python" - "$python_run_heap_profile_path" "$python_perfetto_path" "$python_tmpdir_path" <<'PY'
import importlib.util
import os
import sys
from pathlib import Path

module_path = Path(sys.argv[1])
perfetto_root = Path(sys.argv[2])
script_dir = Path(sys.argv[3])
spec = importlib.util.spec_from_file_location("run_heap_profile", module_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

env = module.build_env(perfetto_root, script_dir=script_dir)
paths = env["PERFETTO_BINARY_PATH"].split(os.pathsep)
expected = [
    str((script_dir / "fs_symbols/arm64-v8a").resolve()),
    str((script_dir / "workspace/allsymbols/arm64-v8a").resolve()),
]
if paths[:2] != expected:
  raise SystemExit(
      "PERFETTO_BINARY_PATH 未按 FS 符号目录优先、旧 allsymbols 补充的顺序生成: "
      + repr(paths))
PY

PERFETTO_BINARY_PATH="$tmpdir/custom_symbols" \
  RUN_HEAP_PROFILE_SYMBOLS_DIR="$tmpdir/fs_symbols/arm64-v8a" \
  "$real_python" - "$python_run_heap_profile_path" "$python_perfetto_path" "$python_tmpdir_path" <<'PY'
import importlib.util
import os
import sys
from pathlib import Path

module_path = Path(sys.argv[1])
perfetto_root = Path(sys.argv[2])
script_dir = Path(sys.argv[3])
spec = importlib.util.spec_from_file_location("run_heap_profile", module_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

env = module.build_env(perfetto_root, script_dir=script_dir)
if env["PERFETTO_BINARY_PATH"] != os.environ["PERFETTO_BINARY_PATH"]:
  raise SystemExit("外部 PERFETTO_BINARY_PATH 应优先于脚本默认符号目录")
PY

fake_rpc_pid=""

start_fake_rpc_server() {
  rm -f "$FAKE_RPC_READY_FILE"
  "$real_python" "$fake_rpc_server_path" \
    >"$tmpdir/fake_rpc_server.out" 2>&1 &
  fake_rpc_pid=$!
  local attempt
  for attempt in $(seq 1 50); do
    if [[ -f "$FAKE_RPC_READY_FILE" ]]; then
      return 0
    fi
    if ! kill -0 "$fake_rpc_pid" 2>/dev/null; then
      cat "$tmpdir/fake_rpc_server.out"
      return 1
    fi
    sleep 0.1
  done
  echo "测试 RPC 服务未及时启动"
  cat "$tmpdir/fake_rpc_server.out"
  return 1
}

stop_fake_rpc_server() {
  if [[ -n "$fake_rpc_pid" ]] && kill -0 "$fake_rpc_pid" 2>/dev/null; then
    kill "$fake_rpc_pid" 2>/dev/null || true
  fi
  if [[ -n "$fake_rpc_pid" ]]; then
    wait "$fake_rpc_pid" 2>/dev/null || true
  fi
  fake_rpc_pid=""
}

run_script() {
  local log_file=$1
  shift
  : >"$log_file"
  printf '0' >"$PIDOF_COUNT_FILE"
  cd /
  start_fake_rpc_server
  local rc
  if FAKE_HEAP_PROFILE_WAIT_FOR_SIGINT=1 \
      "$tmpdir/run_heap_profile.sh" "$@" >"$tmpdir/run_heap_profile_test.out"; then
    rc=0
  else
    rc=$?
  fi
  stop_fake_rpc_server
  return "$rc"
}

run_script_with_timeout() {
  local timeout_s=$1
  local log_file=$2
  shift 2
  : >"$log_file"
  printf '0' >"$PIDOF_COUNT_FILE"
  cd /
  start_fake_rpc_server
  local rc
  if FAKE_HEAP_PROFILE_WAIT_FOR_SIGINT=1 \
      timeout "$timeout_s" "$tmpdir/run_heap_profile.sh" "$@" \
      >"$tmpdir/run_heap_profile_test.out"; then
    rc=0
  else
    rc=$?
  fi
  stop_fake_rpc_server
  return "$rc"
}

wait_for_test_log_pattern() {
  local pattern=$1
  local attempt
  for attempt in $(seq 1 50); do
    if grep -Eq "$pattern" "$TEST_LOG"; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

expected_app="com.example.heapprofile"
default_collection_wait_seconds=$(env -u HEAP_PROFILE_LOGIN_STABLE_S "$real_python" - "$python_action_path" <<'PY'
import runpy
import sys

namespace = runpy.run_path(sys.argv[1])
print(namespace["get_collection_wait_seconds"](None))
PY
)
if [[ "$default_collection_wait_seconds" != "120.0" ]]; then
  echo "默认测试模块最长采集时间必须是 120 秒"
  exit 1
fi
run_script "$TEST_LOG"
settings_get_line=$(grep -n "adb -s FAKE_HEAP_DEVICE shell settings get global hide_error_dialogs" "$TEST_LOG" | head -1 | cut -d: -f1)
settings_put_line=$(grep -n "adb -s FAKE_HEAP_DEVICE shell settings put global hide_error_dialogs 1" "$TEST_LOG" | head -1 | cut -d: -f1)
profile_start_line=$(grep -n "heap_profile.py -n $expected_app" "$TEST_LOG" | head -1 | cut -d: -f1)
settings_restore_line=$(grep -n "adb -s FAKE_HEAP_DEVICE shell settings delete global hide_error_dialogs" "$TEST_LOG" | tail -1 | cut -d: -f1)
if [[ -z "$settings_get_line" || -z "$settings_put_line" || \
      -z "$profile_start_line" || -z "$settings_restore_line" || \
      "$settings_get_line" -ge "$settings_put_line" || \
      "$settings_put_line" -ge "$profile_start_line" || \
      "$profile_start_line" -ge "$settings_restore_line" ]]; then
  echo "Native heap 采集必须先隐藏系统错误对话框，结束后恢复原值"
  cat "$TEST_LOG"
  exit 1
fi
if grep -Fq "shell monkey -p $expected_app 1" "$TEST_LOG"; then
  echo "FS 登录场景采集不能使用 monkey 启动"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "adb shell am start -n $expected_app/com.dhplugin.unity.MainActivity" "$TEST_LOG"; then
  echo "未使用明确 Activity 启动 FS"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "adb logcat -v time" "$TEST_LOG"; then
  echo "未保存登录场景 logcat"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "PYTHON_GOT_SIGINT" "$TEST_LOG"; then
  echo "登录完成后未请求 heap_profile.py 收尾"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "LOGIN_SCENE_DONE|pattern=" "$tmpdir/run_heap_profile_test.out" || \
   ! grep -Fq "PROFILE_ACTION=PASS|reason=wait_elapsed|wait_seconds=1" \
      "$tmpdir/run_heap_profile_test.out"; then
  echo "登录并完成 GM RPC 后必须按测试模块等待时间结束采集"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq \
    "HEAP_PROFILE_CONFIG|app=$expected_app|activity=$expected_app/com.dhplugin.unity.MainActivity|serial=FAKE_HEAP_DEVICE|interval_bytes=1024|shmem_size_bytes=8388608|trace_buffer_kib=63488|gm_ready_timeout_s=180|rpc_local_port=22346|action_script=" \
    "$tmpdir/run_heap_profile_test.out"; then
  echo "启动时未输出完整关键配置"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "adb -s FAKE_HEAP_DEVICE forward tcp:22346 tcp:5002" "$TEST_LOG"; then
  echo "未在配置的设备上把本机测试端口转发到 Poco 实际端口"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq \
    'FAKE_RPC_REQUEST|method=DoRecordCheat|params=["CheatFunc_BatchCheatOption.战斗录像:@40011@@|"]' \
    "$TEST_LOG"; then
  echo "Poco RPC 方法或 GM 参数不正确"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "PROFILE_ACTION_RPC=PASS|method=DoRecordCheat" "$tmpdir/run_heap_profile_test.out"; then
  echo "GM RPC 成功时未输出 PASS"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "HEAP_PROFILE_GM_READY=PASS" "$tmpdir/run_heap_profile_test.out"; then
  echo "延迟表加载完成后未进入 GM 就绪状态"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -R -E -l --include="heap_profile_config.txt" \
    "HEAP_PROFILE_CONFIG\|app=$expected_app.*interval_bytes=1024.*shmem_size_bytes=8388608" \
    "$tmpdir/PerfData/mem" >/dev/null; then
  echo "结果目录中未保存本轮关键配置快照"
  find "$tmpdir/PerfData/mem" -name "heap_profile_config.txt" -print
  exit 1
fi
if ! wait_for_test_log_pattern 'FAKE_LOGCAT_(TERM|PARENT_GONE)'; then
  echo "正常登录完成后未终止长驻 logcat"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "adb shell am force-stop $expected_app" "$TEST_LOG"; then
  echo "采集前未重启目标应用，malloc live 总量无法和 meminfo Native Heap Alloc 对齐"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "heap_profile.py -n $expected_app -o" "$TEST_LOG"; then
  echo "未继续执行 heap profile"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--no-running" "$TEST_LOG"; then
  echo "未在目标进程启动前启用 heapprofd"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--traceconv-binary" "$TEST_LOG"; then
  echo "未显式使用本地构建的 traceconv"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--trace-processor-binary" "$TEST_LOG"; then
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
if ! grep -Eq "PYTHONPATH=.*perfetto.*python" "$TEST_LOG"; then
  echo "未把 Perfetto Python 包目录传给 heap_profile.py"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "PYTHONUNBUFFERED=1" "$TEST_LOG"; then
  echo "未禁用 Python stdout 缓冲，可能导致等待 Profiling active 后才启动应用"
  cat "$TEST_LOG"
  exit 1
fi
if "$real_python" -c 'import os, sys; sys.exit(0 if os.name == "nt" else 1)' &&
  ! grep -Eq 'buildtools(\\|/)win(\\|/)clang(\\|/)bin' "$TEST_LOG"; then
  echo "Windows 下未把 Perfetto clang bin 加入 PATH，traceconv 可能找不到 llvm-symbolizer.exe"
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
if ! grep -Fq "heapprofd_rejected_concurrent" "$TEST_LOG"; then
  echo "健康检查 SQL 应覆盖 heapprofd_rejected_concurrent，便于定位并发 profiling 残留"
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
if ! grep -Fq "heap_dump_count=1" "$tmpdir/run_heap_profile_test.out"; then
  echo "应统计 Perfetto 生成的 heap_dump.*.pb 文件"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
meminfo_line=$(grep -n "adb shell dumpsys meminfo $expected_app" "$TEST_LOG" | head -1 | cut -d: -f1)
profile_log_line=$(grep -n "cp .*heap_profile.log" "$TEST_LOG" | head -1 | cut -d: -f1)
if [[ "$meminfo_line" == "" || "$profile_log_line" == "" || "$meminfo_line" -ge "$profile_log_line" ]]; then
  echo "应在 profiler 进入 shutdown 后、host 后处理完成前抓取 meminfo"
  cat "$TEST_LOG"
  exit 1
fi

export FAKE_PIDOF_NEVER=1
export HEAP_PROFILE_APP_START_TIMEOUT_S=1
export HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S=2
set +e
run_script_with_timeout 8 "$TEST_LOG"
app_start_timeout_rc=$?
set -e
unset FAKE_PIDOF_NEVER HEAP_PROFILE_APP_START_TIMEOUT_S HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S
if [[ "$app_start_timeout_rc" -eq 124 ]]; then
  echo "pidof 永不返回 PID 时脚本不应无限等待"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if [[ "$app_start_timeout_rc" -eq 0 ]]; then
  echo "pidof 永不返回 PID 时脚本不应成功"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "HEAP_PROFILE_FAILED|reason=app_start_timeout" "$tmpdir/run_heap_profile_test.out"; then
  echo "pidof 永不返回 PID 时未输出 app_start_timeout"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! wait_for_test_log_pattern 'FAKE_LOGCAT_(TERM|PARENT_GONE)'; then
  echo "pidof 超时退出时未终止 logcat"
  cat "$TEST_LOG"
  exit 1
fi

export FAKE_AM_START_RC=37
export HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S=2
set +e
run_script_with_timeout 8 "$TEST_LOG"
app_start_failed_rc=$?
set -e
unset FAKE_AM_START_RC HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S
if [[ "$app_start_failed_rc" -eq 124 ]]; then
  echo "am start 失败时脚本不应无限等待"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if [[ "$app_start_failed_rc" -eq 0 ]]; then
  echo "am start 失败时脚本不应成功"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "HEAP_PROFILE_FAILED|reason=app_start_failed|rc=37" "$tmpdir/run_heap_profile_test.out"; then
  echo "am start 失败时未输出 app_start_failed"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi

export FAKE_HEAP_PROFILE_IGNORE_SIGINT=1
export HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S=1
set +e
run_script_with_timeout 12 "$TEST_LOG"
profiler_shutdown_timeout_rc=$?
set -e
unset FAKE_HEAP_PROFILE_IGNORE_SIGINT HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S
if [[ "$profiler_shutdown_timeout_rc" -eq 124 ]]; then
  echo "heap_profile.py 不响应 shutdown 时脚本不应无限等待"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if [[ "$profiler_shutdown_timeout_rc" -eq 0 ]]; then
  echo "heap_profile.py 不响应 shutdown 时脚本不应成功"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "HEAP_PROFILE_FAILED|reason=profiler_shutdown_timeout" "$tmpdir/run_heap_profile_test.out"; then
  echo "heap_profile.py 不响应 shutdown 时未输出 profiler_shutdown_timeout"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi

run_script "$TEST_LOG" 45000
if grep -Fq -- "-d 45000" "$TEST_LOG"; then
  echo "传入历史参数 45000 时不应设置 45 秒自动退出采集时长"
  cat "$TEST_LOG"
  exit 1
fi

run_script "$TEST_LOG" 45000 1024
if grep -Fq -- "-d 45000" "$TEST_LOG"; then
  echo "传入历史参数 45000 时不应设置 45 秒自动退出采集时长"
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

run_script "$TEST_LOG" 128 268435456
if ! grep -Fq -- "-i 128" "$TEST_LOG"; then
  echo "无 duration 模式下第一个参数应作为采样 interval"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--shmem-size 268435456" "$TEST_LOG"; then
  echo "无 duration 模式下第二个参数应作为 heapprofd 共享缓冲区大小"
  cat "$TEST_LOG"
  exit 1
fi

if [[ -z "${MSYSTEM:-}" ]]; then
  export FAKE_HEAP_PROFILE_WAIT_FOR_SIGINT=1
  : >"$TEST_LOG"
  printf '0' >"$PIDOF_COUNT_FILE"
  cd /
  start_fake_rpc_server
  set +e
  "$real_python" - "$tmpdir/run_heap_profile.sh" "$TEST_LOG" "$tmpdir/run_heap_profile_sigint.out" "$expected_app" <<'PY'
import os
import signal
import subprocess
import sys
import time

script, test_log, output_path, expected_app = sys.argv[1:5]

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
        if f"adb shell am start -n {expected_app}/com.dhplugin.unity.MainActivity" in log.read():
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
  stop_fake_rpc_server
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
fi

if "$real_python" -c 'import os, sys; sys.exit(0 if os.name == "nt" else 1)'; then
  bridge_module_path="$tmpdir/run_heap_profile.py"
  bridge_fake_profile="$tmpdir/perfetto/python/tools/heap_profile.py"
  bridge_out_dir="$tmpdir/windows_bridge_out"
  if command -v cygpath >/dev/null 2>&1; then
    bridge_module_path=$(cygpath -w "$bridge_module_path")
    bridge_fake_profile=$(cygpath -w "$bridge_fake_profile")
    bridge_out_dir=$(cygpath -w "$bridge_out_dir")
  fi

  export FAKE_HEAP_PROFILE_WAIT_FOR_SIGINT=1
  : >"$TEST_LOG"
  set +e
  "$real_python" - "$bridge_module_path" "$bridge_fake_profile" "$bridge_out_dir" "$expected_app" <<'PY' >"$tmpdir/run_heap_profile_windows_bridge.out"
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

module_path, fake_profile, out_dir, expected_app = sys.argv[1:5]
sys.path.insert(0, str(Path(module_path).resolve().parent))
spec = importlib.util.spec_from_file_location("run_heap_profile_under_test",
                                              module_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

cmd = module.build_heap_profile_command(
    sys.executable,
    Path(fake_profile),
    ["-n", expected_app, "-o", out_dir])
proc = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    env=os.environ.copy(),
    creationflags=module.profiler_creation_flags())

lines = []

def pump_stdout():
  assert proc.stdout is not None
  for line in proc.stdout:
    lines.append(line)

thread = threading.Thread(target=pump_stdout, daemon=True)
thread.start()

deadline = time.time() + 5
while time.time() < deadline:
  if any("Profiling active" in line for line in lines):
    break
  if proc.poll() is not None:
    break
  time.sleep(0.05)
else:
  proc.terminate()
  proc.wait(timeout=5)
  print("未等到 fake heap_profile.py 进入 Profiling active")
  print("".join(lines))
  sys.exit(125)

module.request_profiler_shutdown(proc)
try:
  rc = proc.wait(timeout=8)
except subprocess.TimeoutExpired:
  proc.terminate()
  proc.wait(timeout=5)
  print("Windows bridge 发出 Ctrl-Break 后 fake heap_profile.py 未退出")
  print("".join(lines))
  sys.exit(124)

thread.join(timeout=2)
output = "".join(lines)
print(output, end="")
if rc != 0:
  sys.exit(rc)
if "Waiting for profiler shutdown" not in output:
  sys.exit(126)
PY
  bridge_rc=$?
  set -e
  unset FAKE_HEAP_PROFILE_WAIT_FOR_SIGINT
  if [[ "$bridge_rc" -ne 0 ]]; then
    echo "Windows 下 Ctrl-Break bridge 应转成 heap_profile.py 的 SIGINT 收尾"
    cat "$TEST_LOG"
    cat "$tmpdir/run_heap_profile_windows_bridge.out"
    exit 1
  fi
  if ! grep -Fq "PYTHON_GOT_SIGINT" "$TEST_LOG"; then
    echo "Windows bridge 未触发 fake heap_profile.py 的 SIGINT handler"
    cat "$TEST_LOG"
    cat "$tmpdir/run_heap_profile_windows_bridge.out"
    exit 1
  fi
  if ! grep -Fq "PYTHON_SIGINT_DONE" "$TEST_LOG"; then
    echo "Windows bridge 未等待 fake heap_profile.py 完成 trace 收尾"
    cat "$TEST_LOG"
    cat "$tmpdir/run_heap_profile_windows_bridge.out"
    exit 1
  fi
fi

export FAKE_LOGCAT_NO_LOGIN=1
export HEAP_PROFILE_LOGIN_TIMEOUT_S=1
set +e
# Windows 下停止假 logcat 最多还会等待 5 秒，外层只用于防止无限挂起。
run_script_with_timeout 10 "$TEST_LOG"
login_timeout_rc=$?
set -e
unset FAKE_LOGCAT_NO_LOGIN HEAP_PROFILE_LOGIN_TIMEOUT_S
if [[ "$login_timeout_rc" -eq 124 ]]; then
  echo "显式设置 HEAP_PROFILE_LOGIN_TIMEOUT_S 后，未进入登录场景时脚本应自行超时退出"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if [[ "$login_timeout_rc" -eq 0 ]]; then
  echo "未进入登录场景时脚本不应验证成功"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "LOGIN_SCENE_TIMEOUT" "$tmpdir/run_heap_profile_test.out"; then
  echo "显式登录超时时应输出 LOGIN_SCENE_TIMEOUT"
  cat "$TEST_LOG"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi

export FAKE_HEAP_PROFILE_RC=1
set +e
run_script "$TEST_LOG"
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
run_script "$TEST_LOG"
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

export FAKE_RPC_ERROR=1
set +e
run_script "$TEST_LOG"
rpc_error_rc=$?
set -e
unset FAKE_RPC_ERROR
if [[ "$rpc_error_rc" -eq 0 ]]; then
  echo "GM RPC 返回错误时脚本不应验证成功"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "PROFILE_ACTION_RPC=FAIL|method=DoRecordCheat" \
      "$tmpdir/run_heap_profile_test.out" || \
   ! grep -Fq "PROFILE_ACTION=FAIL|reason=action_failed" \
      "$tmpdir/run_heap_profile_test.out" || \
   ! grep -Fq "HEAP_PROFILE_FAILED|reason=profile_action_failed" \
      "$tmpdir/run_heap_profile_test.out"; then
  echo "GM RPC 失败时未输出明确阶段错误"
  cat "$tmpdir/run_heap_profile_test.out"
  exit 1
fi
if ! grep -Fq "PYTHON_GOT_SIGINT" "$TEST_LOG"; then
  echo "GM RPC 失败后未请求 Perfetto 正常收尾"
  cat "$TEST_LOG"
  exit 1
fi
