"""00wann 专业采集后端使用的 Perfetto 工具定位。"""

from __future__ import annotations

import os

from device_test_framework.models import RunContext


def resolve_perfetto_tool(context: RunContext, tool: str) -> str:
  configured = getattr(context.config.tools, tool)
  if configured:
    return configured
  root = context.config.tools.perfetto_root
  binary_name = "trace_processor_shell" if tool == "trace_processor" else tool
  if os.name == "nt":
    relatives = [
        f"out/win_clang/{binary_name}.exe",
        f"out/win/{binary_name}.exe",
        f"out/linux_clang_release/{binary_name}",
    ]
  else:
    relatives = [
        f"out/linux_clang_release/{binary_name}",
        f"out/android_arm64/{binary_name}",
        f"out/win_clang/{binary_name}.exe",
    ]
  candidates = [root / relative for relative in relatives]
  for candidate in candidates:
    if candidate.is_file():
      return str(candidate.resolve())
  return str(candidates[0].resolve())
