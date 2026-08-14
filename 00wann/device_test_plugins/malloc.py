"""Native malloc/heapprofd 项目功能插件。"""

from __future__ import annotations

from device_test_framework.features.base import backend_python
from device_test_framework.models import RunContext

from .environment import ProjectBackendFeature


class MallocFeature(ProjectBackendFeature):
  name = "malloc"
  output_category = "mem"
  required_capabilities = frozenset({
      "perfetto", "app_lifecycle", "log_capture", "port_forward",
      "process_memory_snapshot",
  })

  def build_command(self, context: RunContext) -> list[str]:
    args = list(context.config.backend_args)
    options = context.config.feature_options
    if not args:
      args = [
          options.get("interval_bytes", "1024"),
          options.get("shmem_size_bytes", "8388608"),
      ]
    return [
        backend_python(context),
        str(context.config.project_root / "run_heap_profile.py"),
        *args,
    ]

  def build_environment(self, context: RunContext) -> dict[str, str]:
    env = super().build_environment(context)
    env["DEVICE_TEST_OUTPUT_DIR"] = str(context.output_dir)
    return env
