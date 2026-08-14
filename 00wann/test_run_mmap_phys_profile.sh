#!/usr/bin/env bash
set -euo pipefail

tmpdir=$(mktemp -d)
if [[ "${KEEP_TEST_TMP:-0}" == "1" ]]; then
  echo "保留测试临时目录: $tmpdir"
else
  trap 'rm -rf "$tmpdir"' EXIT
fi

script_dir=$(cd "$(dirname "$0")" && pwd)
cp "$script_dir/run_mmap_phys_profile.sh" "$tmpdir/run_mmap_phys_profile.sh"
cp "$script_dir/run_device_test.sh" "$tmpdir/run_device_test.sh"
cp "$script_dir/run_device_test.py" "$tmpdir/run_device_test.py"
cp "$script_dir/device_test.ini" "$tmpdir/device_test.ini"
cp -R "$script_dir/device_test_framework" "$tmpdir/device_test_framework"
cp -R "$script_dir/device_test_plugins" "$tmpdir/device_test_plugins"
cp -R "$script_dir/profile_actions" "$tmpdir/profile_actions"
cp "$script_dir/common_tools.sh" "$tmpdir/common_tools.sh"
cp "$script_dir/debugconfig.txt" "$tmpdir/debugconfig.txt"
cp "$script_dir/FSBootCmdLine.cfg" "$tmpdir/FSBootCmdLine.cfg"
mkdir -p "$tmpdir/heap_analyzer"
cp "$script_dir/heap_analyzer/fs.ini" "$tmpdir/heap_analyzer/fs.ini"

cat >"$tmpdir/config.sh" <<EOF
PerfettoRoot="$tmpdir/perfetto"
export MMAP_PHYS_APP=\${MMAP_PHYS_APP:-com.fs.t.prf}
export ANDROID_SERIAL=\${ANDROID_SERIAL:-FAKE_MMAP_DEVICE}
EOF
cat >"$tmpdir/fsbootcmd_push_to_phone.sh" <<'EOF'
:
EOF

mkdir -p \
  "$tmpdir/bin" \
  "$tmpdir/perfetto/buildtools/linux64/clang/bin" \
  "$tmpdir/perfetto/buildtools/win/clang/bin"
cat >"$tmpdir/bin/adb" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'adb %s\n' "$*" >>"${TEST_LOG:?}"
if [[ "${1:-}" == "-s" ]]; then
  [[ "${2:-}" == "FAKE_MMAP_DEVICE" ]]
  shift 2
fi
if [[ "$1" == "get-state" ]]; then
  printf 'device\n'
elif [[ "$1" == "shell" && "$2" == "settings" && "$3" == "get" ]]; then
  printf 'null\n'
elif [[ "$1" == "push" && "$2" == "debugconfig.txt" && "$3" == "/sdcard/Android/data/com.example.meminfodemo/files" ]]; then
  echo "adb: error: stat failed when trying to push to $3: Permission denied" >&2
  exit 1
fi
EOF
cat >"$tmpdir/bin/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'python3 %s\n' "$*" >>"${TEST_LOG:?}"
printf 'PATH %s\n' "$PATH" >>"${TEST_LOG:?}"
exit "${FAKE_PYTHON_RC:-0}"
EOF
chmod +x "$tmpdir/bin/adb" "$tmpdir/bin/python3"

# 必须在假后端加入 PATH 前锁定框架解释器，否则统一入口不会真正执行。
real_python=$(command -v python3 || command -v python || command -v py)
framework_adb="$tmpdir/bin/adb"
backend_python="$tmpdir/bin/python3"
if command -v cygpath >/dev/null 2>&1; then
  bash_exe=$(cygpath -w "$(command -v bash)")
  cat >"$tmpdir/bin/adb.cmd" <<EOF
@"$bash_exe" "%~dp0adb" %*
EOF
  cat >"$tmpdir/bin/python3.cmd" <<EOF
@"$bash_exe" "%~dp0python3" %*
EOF
  framework_adb=$(cygpath -w "$tmpdir/bin/adb.cmd")
  backend_python=$(cygpath -w "$tmpdir/bin/python3.cmd")
fi
export PATH="$tmpdir/bin:$PATH"
export TEST_LOG="$tmpdir/commands.log"
export DEVICE_TEST_PYTHON="$real_python"
export DEVICE_TEST_ADB="$framework_adb"
export DEVICE_TEST_BACKEND_PYTHON="$backend_python"

cd "$tmpdir"
./run_mmap_phys_profile.sh >"$tmpdir/default.out"

settings_get_line=$(grep -n "adb -s FAKE_MMAP_DEVICE shell settings get global hide_error_dialogs" "$TEST_LOG" | head -1 | cut -d: -f1)
settings_put_line=$(grep -n "adb -s FAKE_MMAP_DEVICE shell settings put global hide_error_dialogs 1" "$TEST_LOG" | head -1 | cut -d: -f1)
collector_line=$(grep -nE "python3 .*collect_mmap_phys_data.py" "$TEST_LOG" | head -1 | cut -d: -f1)
settings_restore_line=$(grep -n "adb -s FAKE_MMAP_DEVICE shell settings delete global hide_error_dialogs" "$TEST_LOG" | tail -1 | cut -d: -f1)
if [[ -z "$settings_get_line" || -z "$settings_put_line" || \
      -z "$collector_line" || -z "$settings_restore_line" || \
      "$settings_get_line" -ge "$settings_put_line" || \
      "$settings_put_line" -ge "$collector_line" || \
      "$collector_line" -ge "$settings_restore_line" ]]; then
  echo "mmap 采集必须先隐藏系统错误对话框，结束后恢复原值"
  cat "$TEST_LOG"
  exit 1
fi

if ! grep -Fq -- "--name com.fs.t.prf" "$TEST_LOG"; then
  echo "config.sh default MMAP_PHYS_APP should be passed to collector"
  cat "$TEST_LOG"
  exit 1
fi

if ! grep -Fq -- "--classify-config heap_analyzer/fs.ini --top-n 0" "$TEST_LOG"; then
  echo "默认入口应启用 fs.ini 分类并输出全部调用栈"
  cat "$TEST_LOG"
  exit 1
fi
if [[ "${OSTYPE:-}" == msys* || -n "${MSYSTEM:-}" ]]; then
  if ! grep -Fq -- "$tmpdir/perfetto/buildtools/win/clang/bin" "$TEST_LOG"; then
    echo "Windows traceconv.exe 应能从 PATH 找到 llvm-symbolizer.exe"
    cat "$TEST_LOG"
    exit 1
  fi
fi

: >"$TEST_LOG"
MMAP_PHYS_APP=com.example.meminfodemo ./run_mmap_phys_profile.sh --no-mmap-callstacks >"$tmpdir/env_override.out"
if ! grep -Fq -- "--name com.example.meminfodemo" "$TEST_LOG"; then
  echo "external MMAP_PHYS_APP should override config.sh default"
  cat "$TEST_LOG"
  exit 1
fi

: >"$TEST_LOG"
./run_mmap_phys_profile.sh --top-n 25 >"$tmpdir/override.out"
default_top_line=$(grep -nF -- "--top-n 0" "$TEST_LOG" | head -1 | cut -d: -f1)
override_top_line=$(grep -nF -- "--top-n 25" "$TEST_LOG" | head -1 | cut -d: -f1)
if [[ -z "$default_top_line" || -z "$override_top_line" || \
      "$default_top_line" -gt "$override_top_line" ]]; then
  echo "显式 --top-n 应保留在默认值之后传给采集脚本，允许 argparse 使用最后一次取值"
  cat "$TEST_LOG"
  exit 1
fi

: >"$TEST_LOG"
./run_mmap_phys_profile.sh --no-mmap-callstacks >"$tmpdir/test.out"

if grep -Fq -- "--malloc" "$TEST_LOG"; then
  echo "无栈验证入口不应传入 malloc/heapprofd 参数"
  cat "$TEST_LOG"
  exit 1
fi
if grep -Fq -- "--malloc-sampling-interval-bytes" "$TEST_LOG"; then
  echo "无栈验证不应传入 malloc sampling interval"
  cat "$TEST_LOG"
  exit 1
fi
if grep -Fq -- "--malloc-shmem-size-bytes" "$TEST_LOG"; then
  echo "无栈验证不应传入 heapprofd shmem 参数"
  cat "$TEST_LOG"
  exit 1
fi
if grep -Fq -- "--max-malloc" "$TEST_LOG"; then
  echo "无栈验证不应启用 heapprofd 自动调参参数"
  cat "$TEST_LOG"
  exit 1
fi

: >"$TEST_LOG"
if FAKE_PYTHON_RC=7 ./run_mmap_phys_profile.sh >"$tmpdir/failure.out" 2>&1; then
  echo "采集器失败时入口应保留失败退出码"
  exit 1
fi
if ! grep -Fq "adb -s FAKE_MMAP_DEVICE shell settings delete global hide_error_dialogs" "$TEST_LOG"; then
  echo "mmap 采集失败时未恢复系统错误对话框设置"
  cat "$TEST_LOG"
  exit 1
fi
