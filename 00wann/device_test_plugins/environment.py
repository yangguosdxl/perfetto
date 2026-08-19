"""00wann 旧配置兼容和专业后端公共环境。"""

from __future__ import annotations

import os

from device_test_framework.config import LegacyEnvironmentMapping
from device_test_framework.features.base import BackendFeature
from device_test_framework.models import ArchiveRequest, OperationResult, RunContext


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

  archive_input_names: tuple[str, ...] = ()
  archive_output_names: tuple[str, ...] = ()

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

  def get_archive_input_names(self, _context: RunContext) -> tuple[str, ...]:
    return self.archive_input_names

  def register_archive_requests(self, context: RunContext) -> None:
    """宿主声明文件语义，通用框架只负责收尾归档。"""
    for name in self.get_archive_input_names(context):
      context.request_archive(ArchiveRequest(
          name,
          context.config.project_root / name,
          "config",
          self.name,
          True,
          False,
          "本轮宿主输入配置",
      ))
    context.request_archive(ArchiveRequest(
        "logcat.txt",
        context.output_dir / "logcat.txt",
        "app_log",
        self.name,
        True,
        True,
        "本轮应用主日志",
    ))
    context.request_archive(ArchiveRequest(
        "logcat.err.txt",
        context.output_dir / "logcat.err.txt",
        "app_log_error",
        self.name,
        False,
        True,
        "应用日志采集进程错误输出",
    ))
    for name in self.archive_output_names:
      context.request_archive(ArchiveRequest(
          name,
          context.output_dir / name,
          "config",
          self.name,
          False,
          True,
          "本轮专业采集配置",
      ))

  def run(self, context: RunContext) -> OperationResult:
    self.register_archive_requests(context)
    return super().run(context)
