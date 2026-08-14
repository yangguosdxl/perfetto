#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"

# 旧参数原样交给统一框架的 mmap 插件；插件负责 FS 文件准备和工具探测。
exec "$script_dir/run_device_test.sh" mmap -- "$@"
