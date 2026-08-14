#!/usr/bin/env python3
"""性能测试公共 Session API 单元测试。"""

import asyncio
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from profile_action_api import (ProfileActionContext, ProfileActionSession,
                                _run_adb_process)


class _FakeSocket:
  """按指定分片返回 Poco 响应，验证精确读取逻辑。"""

  def __init__(self, chunks):
    self.chunks = list(chunks)
    self.sent = b""
    self.timeout = None

  def __enter__(self):
    return self

  def __exit__(self, _exc_type, _exc, _traceback):
    return False

  def settimeout(self, timeout):
    self.timeout = timeout

  def sendall(self, data):
    self.sent += data

  def recv(self, size):
    if not self.chunks:
      return b""
    chunk = self.chunks.pop(0)
    if len(chunk) <= size:
      return chunk
    self.chunks.insert(0, chunk[size:])
    return chunk[:size]


def _rpc_socket(response):
  body = json.dumps(response).encode("utf-8")
  frame = struct.pack("<I", len(body)) + body
  return _FakeSocket([frame[:2], frame[2:5], frame[5:]])


class ProfileActionSessionTest(unittest.IsolatedAsyncioTestCase):

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    self.output_dir = Path(self.temp_dir.name)
    self.logcat_path = self.output_dir / "logcat.txt"
    self.logcat_path.write_text("", encoding="utf-8")
    self.context = ProfileActionContext(
        app="com.example.app",
        pid=1234,
        output_dir=self.output_dir,
        logcat_path=self.logcat_path,
        adb="custom-adb",
        rpc_local_port=12346,
        android_serial="serial-1",
        summary_path=self.output_dir / "run_summary.txt",
    )
    self.session = ProfileActionSession(self.context)

  def tearDown(self):
    self.temp_dir.cleanup()

  async def test_context字段代理和日志写入(self):
    self.assertEqual(self.session.app, "com.example.app")
    self.context.log("PROFILE_ACTION=PASS|reason=test")
    self.assertIn("PROFILE_ACTION=PASS", (
        self.output_dir / "profile_action.log").read_text(encoding="utf-8"))
    self.assertIn("PROFILE_ACTION=PASS", (
        self.output_dir / "run_summary.txt").read_text(encoding="utf-8"))

  @mock.patch("profile_action_api.subprocess.run")
  async def test底层adb自动指定配置设备(self, run):
    run.return_value = subprocess.CompletedProcess([], 0, "", "")
    _run_adb_process(
        self.context, "shell", "pidof", self.context.app,
        timeout_seconds=3.5)
    run.assert_called_once_with(
        ["custom-adb", "-s", "serial-1", "shell", "pidof",
         "com.example.app"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=3.5,
    )

  @mock.patch("profile_action_api._run_adb_process")
  async def test_run_adb_成功返回输出并记录日志(self, run):
    run.return_value = subprocess.CompletedProcess([], 0, "4321\n", "")
    result = await self.session.run_adb(
        "shell", "pidof", self.context.app, timeout_seconds=3.5)
    self.assertTrue(result.success)
    self.assertEqual(result.stdout, "4321\n")
    run.assert_called_once_with(
        self.context, "shell", "pidof", "com.example.app",
        timeout_seconds=3.5)
    self.assertIn("PROFILE_ACTION_ADB=PASS", (
        self.output_dir / "profile_action.log").read_text(encoding="utf-8"))

  @mock.patch("profile_action_api._run_adb_process")
  async def test_run_adb_非零和执行异常只返回失败(self, run):
    run.return_value = subprocess.CompletedProcess([], 7, "输出", "错误")
    result = await self.session.run_adb("shell", "bad")
    self.assertFalse(result.success)
    self.assertEqual(result.returncode, 7)
    self.assertIn("adb_command_failed", result.error)

    run.side_effect = FileNotFoundError("缺少 adb")
    result = await self.session.run_adb("devices")
    self.assertFalse(result.success)
    self.assertIn("FileNotFoundError", result.error)

  @mock.patch("profile_action_api._run_adb_process")
  @mock.patch("profile_action_api._find_poco_port")
  @mock.patch("profile_action_api.socket.create_connection")
  async def test_rpc_初始化一次复用转发并由close清理(
      self, connect, find_port, run_adb):
    connect.side_effect = [
        _rpc_socket({"jsonrpc": "2.0", "id": 1, "result": True}),
        _rpc_socket({"jsonrpc": "2.0", "id": 2, "result": True}),
    ]
    find_port.return_value = 5002
    run_adb.return_value = subprocess.CompletedProcess([], 0, "", "")

    first = await self.session.invoke_rpc("First", request_id=1)
    second = await self.session.invoke_rpc("Second", request_id=2)
    close = await self.session.close()

    self.assertTrue(first.success)
    self.assertTrue(second.success)
    self.assertTrue(close.success)
    find_port.assert_called_once_with(self.context)
    self.assertEqual(run_adb.call_args_list, [
        mock.call(
            self.context, "forward", "--remove", "tcp:12346"),
        mock.call(
            self.context, "forward", "tcp:12346", "tcp:5002"),
        mock.call(
            self.context, "forward", "--remove", "tcp:12346"),
    ])

  @mock.patch("profile_action_api._run_adb_process")
  @mock.patch("profile_action_api._find_poco_port")
  @mock.patch("profile_action_api.socket.create_connection")
  async def test_rpc_默认检查true并写gm详情(
      self, connect, find_port, run_adb):
    fake_socket = _rpc_socket({"jsonrpc": "2.0", "id": 1, "result": False})
    connect.return_value = fake_socket
    find_port.return_value = 5003
    run_adb.return_value = subprocess.CompletedProcess([], 0, "", "")

    result = await self.session.invoke_rpc(
        "DoRecordCheat", ["测试GM"])

    self.assertFalse(result.success)
    self.assertIn("rpc_result_unexpected", result.error)
    detail = (self.output_dir / "gm_rpc.txt").read_text(encoding="utf-8")
    self.assertIn("method=DoRecordCheat", detail)
    self.assertIn("gm=测试GM", detail)
    self.assertIn("remote_port=5003", detail)
    request_size = struct.unpack("<I", fake_socket.sent[:4])[0]
    request = json.loads(fake_socket.sent[4:].decode("utf-8"))
    self.assertEqual(request_size, len(fake_socket.sent) - 4)
    self.assertEqual(request["params"], ["测试GM"])
    await self.session.close()

  @mock.patch("profile_action_api._run_adb_process")
  @mock.patch("profile_action_api._find_poco_port")
  @mock.patch("profile_action_api.socket.create_connection")
  async def test_rpc_可关闭result检查并返回任意响应(
      self, connect, find_port, run_adb):
    connect.return_value = _rpc_socket(
        {"jsonrpc": "2.0", "id": 1, "result": {"value": 42}})
    find_port.return_value = 5001
    run_adb.return_value = subprocess.CompletedProcess([], 0, "", "")
    result = await self.session.invoke_rpc(
        "QueryState", check_result=False, detail_file=None)
    self.assertTrue(result.success)
    self.assertEqual(result.response["result"], {"value": 42})
    await self.session.close()

  @mock.patch("profile_action_api._run_adb_process")
  @mock.patch("profile_action_api._find_poco_port")
  async def test_rpc初始化失败被缓存且不抛异常(
      self, find_port, run_adb):
    find_port.side_effect = RuntimeError("poco_port_missing:pid=1234")
    first = await self.session.invoke_rpc("First")
    second = await self.session.invoke_rpc("Second")
    self.assertFalse(first.success)
    self.assertFalse(second.success)
    self.assertIn("poco_port_missing", first.error)
    find_port.assert_called_once_with(self.context)
    run_adb.assert_not_called()

  @mock.patch("profile_action_api._run_adb_process")
  @mock.patch("profile_action_api._find_poco_port")
  async def test_rpc转发失败只返回失败并在close尽力清理(
      self, find_port, run_adb):
    find_port.return_value = 5002
    run_adb.side_effect = [
        subprocess.CompletedProcess([], 1, "", "没有残留转发"),
        subprocess.CompletedProcess([], 1, "", "端口失败"),
        subprocess.CompletedProcess([], 0, "", ""),
    ]
    result = await self.session.invoke_rpc("Call")
    self.assertFalse(result.success)
    self.assertIn("adb_forward_failed", result.error)
    close = await self.session.close()
    self.assertTrue(close.success)
    self.assertEqual(run_adb.call_count, 3)

  @mock.patch("profile_action_api._run_adb_process")
  @mock.patch("profile_action_api._find_poco_port")
  @mock.patch("profile_action_api.socket.create_connection")
  async def test_rpc协议错误只返回失败且保留阶段(
      self, connect, find_port, run_adb):
    connect.return_value = _FakeSocket([struct.pack("<I", 5), b"12"])
    find_port.return_value = 5002
    run_adb.return_value = subprocess.CompletedProcess([], 0, "", "")
    result = await self.session.invoke_rpc("Truncated")
    self.assertFalse(result.success)
    self.assertEqual(result.stage, "response")
    self.assertIn("rpc_response_truncated", result.error)
    await self.session.close()

  @mock.patch("profile_action_api._run_adb_process")
  @mock.patch("profile_action_api._find_poco_port")
  @mock.patch("profile_action_api.socket.create_connection")
  async def test_rpc详情写入失败只返回失败(
      self, connect, find_port, run_adb):
    connect.return_value = _rpc_socket(
        {"jsonrpc": "2.0", "id": 1, "result": True})
    find_port.return_value = 5002
    run_adb.return_value = subprocess.CompletedProcess([], 0, "", "")
    with mock.patch.object(Path, "write_text", side_effect=OSError("磁盘错误")):
      result = await self.session.invoke_rpc("Call")
    self.assertFalse(result.success)
    self.assertIn("detail_write_failed:OSError:磁盘错误", result.error)
    await self.session.close()

  async def test_wait_for_app_log_只匹配目标pid(self):
    waiter = asyncio.create_task(
        self.session.wait_for_app_log(
            "目标日志", timeout_seconds=0.5,
            poll_interval_seconds=0.01))
    await asyncio.sleep(0.02)
    self.logcat_path.write_text(
        "08-13 12:00:00.000 I/Unity ( 9999): 目标日志\n"
        "08-13 12:00:00.001 I/Unity ( 1234): 目标日志\n",
        encoding="utf-8")
    result = await waiter
    self.assertTrue(result.success)
    self.assertEqual(result.match.pid, 1234)

  async def test_wait_for_app_log_超时只返回失败(self):
    result = await self.session.wait_for_app_log(
        "不存在", timeout_seconds=0.03, poll_interval_seconds=0.01)
    self.assertFalse(result.success)
    self.assertIn("AppLogTimeoutError", result.error)

  async def test_wait_for_app_log_取消直接传播(self):
    waiter = asyncio.create_task(
        self.session.wait_for_app_log(
            "永不出现", poll_interval_seconds=0.01))
    await asyncio.sleep(0.02)
    waiter.cancel()
    with self.assertRaises(asyncio.CancelledError):
      await waiter

  async def test非法调用参数仍抛ValueError(self):
    with self.assertRaises(ValueError):
      await self.session.invoke_rpc("")
    with self.assertRaises(ValueError):
      await self.session.run_adb("devices", timeout_seconds=0)
    with self.assertRaises(ValueError):
      await self.session.wait_for_app_log(
          "日志", poll_interval_seconds=0)


if __name__ == "__main__":
  unittest.main()
