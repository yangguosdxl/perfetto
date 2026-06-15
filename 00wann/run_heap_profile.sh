#!/usr/bin/env bash
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/common_tools.sh"
python_bin="${RUN_HEAP_PROFILE_PYTHON:-$(select_python)}"
script_path="$script_dir/run_heap_profile.py"
if is_windows_git_bash && command -v cygpath >/dev/null 2>&1; then
  script_path=$(cygpath -w "$script_path")
fi
exec "$python_bin" "$script_path" "$@"
