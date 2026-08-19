"""mmap 真实物理内存归因项目功能插件。"""

from __future__ import annotations

import os

from device_test_framework.features.base import backend_python
from device_test_framework.models import OperationResult, RunContext

from .environment import ProjectBackendFeature
from .perfetto_tools import resolve_perfetto_tool


class MmapFeature(ProjectBackendFeature):
  name = "mmap"
  output_category = "mmap_phys"
  archive_input_names = ("FSBootCmdLine.cfg",)
  archive_output_names = (
      "mmap_phys_config.pbtxt",
      "mmap_lifecycle_config.pbtxt",
      "mmap_callstack_config.pbtxt",
  )
  fs_apps = frozenset({
      "com.fs.t.prf", "com.tencent.dhwdxkty.trunk.profiler"})
  required_capabilities = frozenset({
      "perfetto", "app_lifecycle", "log_capture", "file_transfer",
      "port_forward", "process_memory_snapshot",
  })

  def validate(self, context: RunContext) -> OperationResult:
    result = super().validate(context)
    if not result.success:
      return result
    # 启动配置始终需要；调试配置只属于正式 FS 包。
    boot = context.config.project_root / "FSBootCmdLine.cfg"
    debug = context.config.project_root / "debugconfig.txt"
    if not boot.is_file():
      return OperationResult(
          False, "feature_validate", f"mmap_boot_config_missing:{boot}")
    if context.config.app_id in self.fs_apps and not debug.is_file():
      return OperationResult(
          False, "feature_validate", f"mmap_debug_config_missing:{debug}")
    return OperationResult(True, "feature_validate")

  def build_environment(self, context: RunContext) -> dict[str, str]:
    env = super().build_environment(context)
    env["PYTHONUNBUFFERED"] = "1"
    env["PERFETTO_SYMBOLIZER_MODE"] = "index"
    env.setdefault("PERFETTO_BINARY_PATH", "./workspace/allsymbols/arm64-v8a")
    root = context.config.tools.perfetto_root
    clang_paths = [
        root / "buildtools/win/clang/bin",
        root / "buildtools/linux64/clang/bin",
    ]
    env["PATH"] = os.pathsep.join(
        [str(path.resolve()) for path in clang_paths] + [env.get("PATH", "")])
    root_perf = context.config.feature_options.get("use_root_traced_perf")
    if root_perf:
      env["MMAP_PHYS_USE_ROOT_TRACED_PERF"] = (
          "1" if root_perf.lower() in ("1", "true", "yes", "on") else "0")
    return env

  def get_archive_input_names(self, context: RunContext) -> tuple[str, ...]:
    names = list(super().get_archive_input_names(context))
    if context.config.app_id in self.fs_apps:
      names.append("debugconfig.txt")
    return tuple(names)

  def build_command(self, context: RunContext) -> list[str]:
    options = context.config.feature_options
    args = [
        backend_python(context),
        str(context.config.project_root / "collect_mmap_phys_data.py"),
        "--name", context.config.app_id,
        "--output", str(context.output_dir),
        "--buffer-kb", options.get("buffer_kib", "262144"),
        "--smaps-interval-ms", options.get("smaps_interval_ms", "1000"),
        "--perf-ring-buffer-pages",
        options.get("perf_ring_buffer_pages", "32768"),
        "--perf-ring-buffer-read-period-ms",
        options.get("perf_ring_buffer_read_period_ms", "25"),
        "--classify-config", "heap_analyzer/fs.ini",
        "--top-n", "0",
        "--trace-processor", resolve_perfetto_tool(context, "trace_processor"),
        "--traceconv", resolve_perfetto_tool(context, "traceconv"),
    ]
    args.extend(context.config.backend_args)
    return args

  def run(self, context: RunContext) -> OperationResult:
    boot = context.platform.push_file(
        context.config.project_root / "FSBootCmdLine.cfg",
        "/data/local/tmp/FSBootCmdLine.cfg")
    if not boot.success:
      return boot
    if context.config.app_id in self.fs_apps:
      debug = context.platform.push_file(
          context.config.project_root / "debugconfig.txt",
          f"/sdcard/Android/data/{context.config.app_id}/files")
      if not debug.success:
        return debug
    return super().run(context)
