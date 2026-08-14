#!/usr/bin/env python3
"""性能测试模块协程竞速执行器单元测试。"""

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from profile_action_api import (ActionOperationResult, ProfileActionContext,
                                ProfileActionSession)
from profile_action_runner import run_profile_action_module


class ProfileActionRunnerTest(unittest.TestCase):

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    output_dir = Path(self.temp_dir.name)
    self.context = ProfileActionContext(
        app="com.example.app",
        pid=1234,
        output_dir=output_dir,
        logcat_path=output_dir / "logcat.txt",
        adb="adb",
        rpc_local_port=12346,
        android_serial="serial",
    )

  def tearDown(self):
    self.temp_dir.cleanup()

  def _module(self, wait_value, action):
    return types.SimpleNamespace(
        get_collection_wait_seconds=lambda _context: wait_value,
        run_profile_action=action,
    )

  def test_协程正常完成(self):
    received = []

    async def action(session):
      received.append(session)
      return None

    result = run_profile_action_module(
        self._module(10, action), self.context, lambda: False, lambda: True)
    self.assertTrue(result.success)
    self.assertEqual(result.reason, "action_completed")
    self.assertIsInstance(received[0], ProfileActionSession)
    self.assertIs(received[0].context, self.context)

  def test_不调用rpc时不初始化poco(self):
    async def action(_session):
      return None

    with mock.patch("profile_action_api._find_poco_port") as find_port:
      result = run_profile_action_module(
          self._module(10, action), self.context, lambda: False, lambda: True)
    self.assertTrue(result.success)
    find_port.assert_not_called()

  def test_协程异常后仍关闭公共会话(self):
    async def action(_session):
      raise RuntimeError("测试异常")

    close = mock.AsyncMock(
        return_value=ActionOperationResult(True, "close"))
    with mock.patch.object(ProfileActionSession, "close", new=close):
      result = run_profile_action_module(
          self._module(10, action), self.context, lambda: False, lambda: True)
    self.assertFalse(result.success)
    close.assert_awaited_once()

  def test_零秒等待取消协程并执行清理(self):
    states = []

    async def action(_context):
      try:
        states.append("started")
        import asyncio
        await asyncio.Event().wait()
      finally:
        states.append("cleaned")

    result = run_profile_action_module(
        self._module(0, action), self.context, lambda: False, lambda: True)
    self.assertTrue(result.success)
    self.assertEqual(result.reason, "wait_elapsed")
    self.assertEqual(states, ["started", "cleaned"])

  def test_none_等待协程完成(self):
    async def action(_context):
      return None

    result = run_profile_action_module(
        self._module(None, action), self.context, lambda: False, lambda: True)
    self.assertEqual(result.reason, "action_completed")

  def test_协程异常导致失败(self):
    async def action(_context):
      raise RuntimeError("测试异常")

    result = run_profile_action_module(
        self._module(10, action), self.context, lambda: False, lambda: True)
    self.assertFalse(result.success)
    self.assertEqual(result.reason, "action_failed")
    self.assertIn("测试异常", result.error)

  def test_非法等待值导致失败(self):
    async def action(_context):
      return None

    for value in (-1, float("nan"), float("inf"), "1", True):
      with self.subTest(value=value):
        result = run_profile_action_module(
            self._module(value, action), self.context, lambda: False,
            lambda: True)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "invalid_wait_seconds")

  def test_app_死亡会取消协程并失败(self):
    states = []

    async def action(_context):
      try:
        import asyncio
        await asyncio.Event().wait()
      finally:
        states.append("cleaned")

    result = run_profile_action_module(
        self._module(None, action), self.context, lambda: False, lambda: False)
    self.assertFalse(result.success)
    self.assertEqual(result.reason, "app_died")
    self.assertEqual(states, ["cleaned"])

  def test_人工中断会先取消协程并正常结束(self):
    states = []

    async def action(_context):
      try:
        import asyncio
        await asyncio.Event().wait()
      finally:
        states.append("cleaned")

    result = run_profile_action_module(
        self._module(None, action), self.context, lambda: True, lambda: True)
    self.assertTrue(result.success)
    self.assertEqual(result.reason, "interrupted")
    self.assertEqual(states, ["cleaned"])


if __name__ == "__main__":
  unittest.main()
