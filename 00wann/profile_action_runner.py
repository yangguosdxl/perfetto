#!/usr/bin/env python3
"""加载并运行配置的性能测试协程。"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import math
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

from profile_action_api import ProfileActionContext, ProfileActionSession


@dataclass(frozen=True)
class ProfileActionResult:
  success: bool
  reason: str
  wait_seconds: float | None
  error: str = ""


def resolve_action_module_path(script_dir: Path) -> Path:
  import os
  configured = os.environ.get(
      "PERF_PROFILE_ACTION_SCRIPT",
      "profile_actions/send_battle_record_gm.py",
  ).strip()
  if not configured:
    raise RuntimeError("PERF_PROFILE_ACTION_SCRIPT 为空")
  path = Path(configured)
  if not path.is_absolute():
    path = script_dir / path
  path = path.resolve()
  if path.suffix.lower() != ".py":
    raise RuntimeError(f"测试模块必须是 .py 文件: {path}")
  if not path.is_file():
    raise RuntimeError(f"测试模块不存在: {path}")
  return path


def load_action_module(path: Path) -> ModuleType:
  module_name = f"perf_profile_action_{abs(hash(path))}"
  spec = importlib.util.spec_from_file_location(module_name, path)
  if spec is None or spec.loader is None:
    raise RuntimeError(f"无法加载测试模块: {path}")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  action = getattr(module, "run_profile_action", None)
  wait_getter = getattr(module, "get_collection_wait_seconds", None)
  if not inspect.iscoroutinefunction(action):
    raise RuntimeError("测试模块必须提供 async run_profile_action(session)")
  if not callable(wait_getter) or inspect.iscoroutinefunction(wait_getter):
    raise RuntimeError(
        "测试模块必须提供同步 get_collection_wait_seconds(session)")
  return module


def get_wait_seconds(module: ModuleType,
                     session: ProfileActionSession) -> float | None:
  value = module.get_collection_wait_seconds(session)
  if value is None:
    return None
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise RuntimeError("get_collection_wait_seconds 必须返回数字或 None")
  value = float(value)
  if not math.isfinite(value) or value < 0:
    raise RuntimeError("get_collection_wait_seconds 必须返回非负有限数或 None")
  return value


async def _cancel_and_wait(task: asyncio.Task[None]) -> None:
  if task.done():
    return
  task.cancel()
  try:
    await task
  except asyncio.CancelledError:
    pass


async def _run_action_race(
    module: ModuleType,
    session: ProfileActionSession,
    wait_seconds: float | None,
    stop_requested: Callable[[], bool],
    process_alive: Callable[[], bool],
) -> ProfileActionResult:
  action_task = asyncio.create_task(module.run_profile_action(session))
  # 先让测试协程进入首个 await，确保零秒等待也能建立 finally 清理区。
  await asyncio.sleep(0)
  loop = asyncio.get_running_loop()
  deadline = loop.time() + wait_seconds if wait_seconds is not None else None
  next_liveness_check = 0.0

  while True:
    if action_task.done():
      try:
        action_task.result()
      except asyncio.CancelledError:
        return ProfileActionResult(True, "cancelled", wait_seconds)
      except Exception as exc:  # noqa: BLE001 - 测试模块异常要转成本轮结果。
        return ProfileActionResult(
            False, "action_failed", wait_seconds,
            f"{type(exc).__name__}: {exc}")
      return ProfileActionResult(True, "action_completed", wait_seconds)

    if stop_requested():
      await _cancel_and_wait(action_task)
      return ProfileActionResult(True, "interrupted", wait_seconds)

    now = loop.time()
    if deadline is not None and now >= deadline:
      await _cancel_and_wait(action_task)
      return ProfileActionResult(True, "wait_elapsed", wait_seconds)

    if now >= next_liveness_check:
      if not process_alive():
        await _cancel_and_wait(action_task)
        return ProfileActionResult(False, "app_died", wait_seconds)
      next_liveness_check = now + 1.0

    sleep_seconds = 0.2
    if deadline is not None:
      sleep_seconds = min(sleep_seconds, max(0.0, deadline - now))
    await asyncio.sleep(sleep_seconds)


def run_profile_action_module(
    module: ModuleType,
    context: ProfileActionContext,
    stop_requested: Callable[[], bool],
    process_alive: Callable[[], bool],
) -> ProfileActionResult:
  """同步采集器使用的协程竞速入口。"""
  session = ProfileActionSession(context)
  try:
    wait_seconds = get_wait_seconds(module, session)
  except Exception as exc:  # noqa: BLE001 - 契约错误需输出结构化结果。
    result = ProfileActionResult(
        False, "invalid_wait_seconds", None,
        f"{type(exc).__name__}: {exc}")
  else:
    context.log(
        "PROFILE_ACTION_WAIT|seconds=" +
        ("none" if wait_seconds is None else f"{wait_seconds:g}"))
    async def run_and_close() -> ProfileActionResult:
      try:
        return await _run_action_race(
            module, session, wait_seconds, stop_requested, process_alive)
      finally:
        await session.close()

    result = asyncio.run(run_and_close())

  status = "PASS" if result.success else "FAIL"
  line = f"PROFILE_ACTION={status}|reason={result.reason}"
  if result.wait_seconds is not None:
    line += f"|wait_seconds={result.wait_seconds:g}"
  if result.error:
    line += f"|error={result.error}"
  context.log(line)
  return result
