#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
script="$script_dir/run_heap_startup_eval.sh"

if [[ "$(head -n 1 "$script")" != "#!/usr/bin/env bash" ]]; then
  echo "run_heap_startup_eval.sh 缺少 bash shebang"
  exit 1
fi

default_output=$(HEAP_STARTUP_DRY_RUN=1 "$script")
if ! grep -Fq "CONFIG|duration_ms=45000|shmem_size=268435456|intervals=512 256 128 64 32 16" <<<"$default_output"; then
  echo "默认启动评估参数不符合预期"
  printf '%s\n' "$default_output"
  exit 1
fi
if ! grep -Fq "BASELINE|pattern=LAN 更新流程开始" <<<"$default_output"; then
  echo "默认启动完成日志不符合预期"
  printf '%s\n' "$default_output"
  exit 1
fi
if ! grep -Fq "CASE|interval=512|duration_ms=45000|shmem_size=268435456" <<<"$default_output"; then
  echo "默认 interval 列表未包含 512"
  printf '%s\n' "$default_output"
  exit 1
fi

custom_output=$(HEAP_STARTUP_DRY_RUN=1 "$script" 30000 67108864 1024 2048)
if ! grep -Fq "CONFIG|duration_ms=30000|shmem_size=67108864|intervals=1024 2048" <<<"$custom_output"; then
  echo "自定义启动评估参数不符合预期"
  printf '%s\n' "$custom_output"
  exit 1
fi
if ! grep -Fq "CASE|interval=2048|duration_ms=30000|shmem_size=67108864" <<<"$custom_output"; then
  echo "自定义 interval 列表未正确传递"
  printf '%s\n' "$custom_output"
  exit 1
fi
