#!/usr/bin/env bash
set -euo pipefail

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

script_dir=$(cd "$(dirname "$0")" && pwd)
cp "$script_dir/run_mmap_phys_profile.sh" "$tmpdir/run_mmap_phys_profile.sh"
cp "$script_dir/common_tools.sh" "$tmpdir/common_tools.sh"
cp "$script_dir/debugconfig.txt" "$tmpdir/debugconfig.txt"
cp "$script_dir/FSBootCmdLine.cfg" "$tmpdir/FSBootCmdLine.cfg"
mkdir -p "$tmpdir/heap_analyzer"
cp "$script_dir/heap_analyzer/fs.ini" "$tmpdir/heap_analyzer/fs.ini"

cat >"$tmpdir/config.sh" <<EOF
PerfettoRoot="$tmpdir/perfetto"
export MMAP_PHYS_APP=\${MMAP_PHYS_APP:-com.fs.t.prf}
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
if [[ "$1" == "push" && "$2" == "debugconfig.txt" && "$3" == "/sdcard/Android/data/com.example.meminfodemo/files" ]]; then
  echo "adb: error: stat failed when trying to push to $3: Permission denied" >&2
  exit 1
fi
EOF
cat >"$tmpdir/bin/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'python3 %s\n' "$*" >>"${TEST_LOG:?}"
printf 'PATH %s\n' "$PATH" >>"${TEST_LOG:?}"
EOF
chmod +x "$tmpdir/bin/adb" "$tmpdir/bin/python3"

export PATH="$tmpdir/bin:$PATH"
export TEST_LOG="$tmpdir/commands.log"

cd "$tmpdir"
./run_mmap_phys_profile.sh >"$tmpdir/default.out"

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
if ! grep -Fq -- "--classify-config heap_analyzer/fs.ini --top-n 0 --top-n 25" "$TEST_LOG"; then
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
