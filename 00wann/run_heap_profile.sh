#!/usr/bin/env bash
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"

# 保留旧位置参数，由统一框架的 malloc 插件转交专业后端。
"$script_dir/run_device_test.sh" malloc -- "$@"
profile_rc=$?
if ((profile_rc != 0)); then
  echo "HEAP_PROFILE_POST_ANALYSIS=SKIP|reason=profile_failed|rc=$profile_rc"
  exit "$profile_rc"
fi

echo "HEAP_PROFILE_POST_ANALYSIS=START"
"$script_dir/run_heap_alloc_stacks_by_symbol_latest.sh"
analysis_rc=$?
if ((analysis_rc != 0)); then
  echo "HEAP_PROFILE_POST_ANALYSIS=FAIL|rc=$analysis_rc"
  exit "$analysis_rc"
fi

echo "HEAP_PROFILE_POST_ANALYSIS=PASS"
