#!/usr/bin/env bash
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"

# 保留旧位置参数，由统一框架的 malloc 插件转交专业后端。
exec "$script_dir/run_device_test.sh" malloc -- "$@"
