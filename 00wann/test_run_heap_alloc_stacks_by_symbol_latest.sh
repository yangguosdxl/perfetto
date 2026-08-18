#!/usr/bin/env bash
set -euo pipefail

tmpdir=$(mktemp -d)
extra_tmpdir=
trap 'rm -rf "$tmpdir" "$extra_tmpdir"' EXIT

script_dir=$(cd "$(dirname "$0")" && pwd)
cp "$script_dir/run_heap_alloc_stacks_by_symbol_latest.sh" \
  "$tmpdir/run_heap_alloc_stacks_by_symbol_latest.sh"
cp "$script_dir/common_tools.sh" "$tmpdir/common_tools.sh"
cp "$script_dir/config.sh" "$tmpdir/config.sh"

mkdir -p "$tmpdir/bin" "$tmpdir/heap_analyzer"
cp "$script_dir/heap_analyzer/fs.ini" "$tmpdir/heap_analyzer/fs.ini"
touch "$tmpdir/heap_analyzer/query_heap_alloc_stacks_by_symbol.py"

cat >"$tmpdir/bin/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'python3 %s\n' "$*" >>"${TEST_LOG:?}"
EOF
chmod +x "$tmpdir/bin/python3"

mkdir -p \
  "$tmpdir/PerfData/mem/2026-06-04_19-04-55" \
  "$tmpdir/PerfData/mem/2026-06-05_11-22-33"
touch -t 202606041904 "$tmpdir/PerfData/mem/2026-06-04_19-04-55/symbolized-trace"
touch -t 202606051122 "$tmpdir/PerfData/mem/2026-06-05_11-22-33/raw-trace"
touch -t 202606051123 "$tmpdir/PerfData/mem/2026-06-05_11-22-33/symbolized-trace"

export PATH="$tmpdir/bin:$PATH"
export TEST_LOG="$tmpdir/commands.log"

cd "$tmpdir"
./run_heap_alloc_stacks_by_symbol_latest.sh --limit 25 >"$tmpdir/default.out"

if ! grep -Fq -- "--trace PerfData/mem/2026-06-05_11-22-33/symbolized-trace" "$TEST_LOG"; then
  echo "wrapper 应默认使用 PerfData/mem 中最新的 symbolized-trace"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--classify-config heap_analyzer/fs.ini" "$TEST_LOG"; then
  echo "wrapper 应默认指定 fs.ini 分类配置"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--all-allocations" "$TEST_LOG"; then
  echo "wrapper 应默认分析全部 allocation"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--limit 0 --limit 25" "$TEST_LOG"; then
  echo "wrapper 应把用户参数放在默认 limit 后面，允许覆盖默认值"
  cat "$TEST_LOG"
  exit 1
fi

: >"$TEST_LOG"
./run_heap_alloc_stacks_by_symbol_latest.sh \
  --symbol malloc \
  --limit 5 >"$tmpdir/symbol.out"

if grep -Fq -- "--all-allocations" "$TEST_LOG"; then
  echo "显式 --symbol 应覆盖默认全量分析，避免 wrapper 限制后续按符号查询"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--symbol malloc" "$TEST_LOG"; then
  echo "显式 --symbol 应原样透传给 Python 查询脚本"
  cat "$TEST_LOG"
  exit 1
fi

: >"$TEST_LOG"
./run_heap_alloc_stacks_by_symbol_latest.sh \
  --trace custom-symbolized-trace \
  --classify-config custom.ini >"$tmpdir/override.out"

if ! grep -Fq -- "--trace PerfData/mem/2026-06-05_11-22-33/symbolized-trace" "$TEST_LOG"; then
  echo "wrapper 应提供最新 trace 作为默认值"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--trace custom-symbolized-trace" "$TEST_LOG"; then
  echo "用户显式 --trace 应追加在默认 trace 后以覆盖默认值"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq -- "--classify-config heap_analyzer/fs.ini --all-allocations --limit 0 --trace custom-symbolized-trace --classify-config custom.ini" "$TEST_LOG"; then
  echo "用户参数顺序应保留在默认参数后面"
  cat "$TEST_LOG"
  exit 1
fi

if { [[ "${OSTYPE:-}" == msys* ]] || [[ -n "${MSYSTEM:-}" ]]; } &&
    command -v cygpath >/dev/null 2>&1; then
  extra_tmpdir=$(mktemp -d "$script_dir/.tmp_win_python.XXXXXX")
  mkdir -p "$extra_tmpdir/bin"
  cat >"$extra_tmpdir/bin/python" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'windows-python %s\n' "$*" >>"${TEST_LOG:?}"
EOF
  chmod +x "$extra_tmpdir/bin/python"

  : >"$TEST_LOG"
  PYTHON="$extra_tmpdir/bin/python" \
    ./run_heap_alloc_stacks_by_symbol_latest.sh >"$tmpdir/windows_python.out"

  expected_script=$(cygpath -w "$tmpdir/heap_analyzer/query_heap_alloc_stacks_by_symbol.py")
  if ! grep -Fq -- "$expected_script" "$TEST_LOG"; then
    echo "Windows 原生 Python 应收到 cygpath 转换后的查询脚本路径"
    echo "期望: $expected_script"
    cat "$TEST_LOG"
    exit 1
  fi
  if grep -Fq -- "$tmpdir/heap_analyzer/query_heap_alloc_stacks_by_symbol.py" "$TEST_LOG"; then
    echo "Windows 原生 Python 不应收到 Git Bash 的 /d/... 脚本路径"
    cat "$TEST_LOG"
    exit 1
  fi
fi
