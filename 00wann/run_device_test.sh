#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"

if [[ ! -f "$script_dir/config.sh" ]]; then
  echo "DEVICE_TEST=FAIL|reason=config_missing|path=$script_dir/config.sh" >&2
  exit 1
fi

# 兼容现有本地测试覆盖；Python 层再将最终环境值归一化。
source "$script_dir/config.sh"
source "$script_dir/common_tools.sh"
export PerfettoRoot MMAP_PHYS_APP ANDROID_SERIAL PERF_PROFILE_ACTION_SCRIPT

python_bin=${DEVICE_TEST_PYTHON:-${RUN_HEAP_PROFILE_PYTHON:-$(select_python)}}
entry="$script_dir/run_device_test.py"
if is_windows_git_bash && command -v cygpath >/dev/null 2>&1; then
  entry=$(cygpath -w "$entry")
fi
exec "$python_bin" "$entry" "$@"
