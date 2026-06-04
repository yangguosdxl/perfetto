#!/usr/bin/env bash
set -euo pipefail

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

script_dir=$(cd "$(dirname "$0")" && pwd)
cp "$script_dir/run_heap_profile.sh" "$tmpdir/run_heap_profile.sh"
cp "$script_dir/debugconfig.txt" "$tmpdir/debugconfig.txt"

if [[ "$(head -n 1 "$tmpdir/run_heap_profile.sh")" != "#!/usr/bin/env bash" ]]; then
  echo "run_heap_profile.sh 缺少 bash shebang，直接执行时可能被 /bin/sh 解释"
  exit 1
fi

# 构造最小运行环境，避免测试依赖真实 Perfetto 输出目录和 Android 设备。
cat >"$tmpdir/config.sh" <<EOF
PerfettoRoot="$tmpdir/perfetto"
EOF
cat >"$tmpdir/fsbootcmd_push_to_phone.sh" <<'EOF'
:
EOF

mkdir -p "$tmpdir/bin" "$tmpdir/perfetto/buildtools/linux64/clang/bin"
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
EOF
chmod +x "$tmpdir/bin/adb" "$tmpdir/bin/cp" "$tmpdir/bin/python3"

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
if ! grep -Fq "python3 $tmpdir/perfetto/python/tools/heap_profile.py -n $expected_app -o" "$TEST_LOG"; then
  echo "未继续执行 heap profile"
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
if ! grep -Fq -- "-i 16" "$TEST_LOG"; then
  echo "默认采集应设置采样 interval 为 16"
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
