#!/usr/bin/env bash
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
python_bin="${RUN_HEAP_PROFILE_PYTHON:-/usr/bin/python3}"
exec "$python_bin" "$script_dir/run_heap_profile.py" "$@"
