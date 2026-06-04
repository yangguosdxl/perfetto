#!/usr/bin/env bash
set -euo pipefail

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

script_dir=$(cd "$(dirname "$0")" && pwd)
cp "$script_dir/run_mmap_phys_profile.sh" "$tmpdir/run_mmap_phys_profile.sh"
cp "$script_dir/debugconfig.txt" "$tmpdir/debugconfig.txt"
cp "$script_dir/FSBootCmdLine.cfg" "$tmpdir/FSBootCmdLine.cfg"

cat >"$tmpdir/config.sh" <<EOF
PerfettoRoot="$tmpdir/perfetto"
EOF
cat >"$tmpdir/fsbootcmd_push_to_phone.sh" <<'EOF'
:
EOF

mkdir -p "$tmpdir/bin" "$tmpdir/perfetto/buildtools/linux64/clang/bin"
cat >"$tmpdir/bin/adb" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'adb %s\n' "$*" >>"${TEST_LOG:?}"
EOF
cat >"$tmpdir/bin/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'python3 %s\n' "$*" >>"${TEST_LOG:?}"
EOF
chmod +x "$tmpdir/bin/adb" "$tmpdir/bin/python3"

export PATH="$tmpdir/bin:$PATH"
export TEST_LOG="$tmpdir/commands.log"

cd "$tmpdir"
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
