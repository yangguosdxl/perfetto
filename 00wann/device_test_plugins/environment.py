"""00wann 旧配置兼容和专业后端公共环境。"""

from __future__ import annotations

import os

from device_test_framework.config import LegacyEnvironmentMapping
from device_test_framework.features.base import BackendFeature
from device_test_framework.models import RunContext


LEGACY_ENVIRONMENT = LegacyEnvironmentMapping(
    device_id=("ANDROID_SERIAL",),
    app_id=("MMAP_PHYS_APP",),
    launch_id=("MMAP_PHYS_ACTIVITY",),
    action_script=("PERF_PROFILE_ACTION_SCRIPT",),
    perfetto_root=("PerfettoRoot",),
    trace_processor=("TRACE_PROCESSOR",),
    traceconv=("TRACECONV",),
    adb=("ADB_BINARY",),
    backend_python=("RUN_HEAP_PROFILE_PYTHON",),
    rpc_local_port=("HEAP_PROFILE_RPC_LOCAL_PORT",),
    rpc_timeout_seconds=("HEAP_PROFILE_RPC_TIMEOUT_S",),
)


def initialize_project_environment() -> None:
  """在读取配置前一次性归一化仅属于 FS 项目的旧变量。"""
  app_id = (
      os.environ.get("DEVICE_TEST_APP_ID") or
      os.environ.get("MMAP_PHYS_APP", "")).strip()
  if app_id and not (
      os.environ.get("DEVICE_TEST_LAUNCH_ID") or
      os.environ.get("MMAP_PHYS_ACTIVITY")):
    os.environ["DEVICE_TEST_LAUNCH_ID"] = (
        f"{app_id}/com.dhplugin.unity.MainActivity")


class ProjectBackendFeature(BackendFeature):
  """向现有专业采集器提供归一化后的项目环境。"""

  def build_environment(self, context: RunContext) -> dict[str, str]:
    env = super().build_environment(context)
    config = context.config
    env.update({
        "MMAP_PHYS_APP": config.app_id,
        "MMAP_PHYS_ACTIVITY": config.launch_id,
        "PerfettoRoot": str(config.tools.perfetto_root),
        "PERF_PROFILE_ACTION_SCRIPT": config.action_script,
        "HEAP_PROFILE_RPC_LOCAL_PORT": str(config.rpc.local_port),
        "HEAP_PROFILE_RPC_TIMEOUT_S": str(config.rpc.timeout_seconds),
    })
    return env
