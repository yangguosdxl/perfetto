#!/usr/bin/env python3
"""性能采集测试模块的稳定公共 API。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import struct
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Pattern

LOGCAT_PID_PATTERN = re.compile(r"\(\s*(\d+)\)")
POCO_PORT_PATTERN = re.compile(r"Tcp server started and listening at (\d+)")


class AppLogTimeoutError(TimeoutError):
  """等待目标 App 日志超时。"""


@dataclass(frozen=True)
class AppLogMatch:
  line: str
  matched_text: str
  pid: int


@dataclass(frozen=True)
class ActionOperationResult:
  success: bool
  operation: str
  error: str = ""


@dataclass(frozen=True)
class CommandResult(ActionOperationResult):
  returncode: int | None = None
  stdout: str = ""
  stderr: str = ""


@dataclass(frozen=True)
class RpcResult(ActionOperationResult):
  response: dict[str, Any] | None = None
  local_port: int = 0
  remote_port: int = 0
  method: str = ""
  stage: str = ""


@dataclass(frozen=True)
class AppLogResult(ActionOperationResult):
  match: AppLogMatch | None = None


@dataclass(frozen=True)
class ProfileActionContext:
  app: str
  pid: int
  output_dir: Path
  logcat_path: Path
  adb: str
  rpc_local_port: int
  android_serial: str
  summary_path: Path | None = None
  _log_lock: threading.Lock = field(
      default_factory=threading.Lock, repr=False, compare=False)

  def log(self, message: str) -> None:
    """同时输出到控制台、本轮测试操作日志和可选汇总日志。"""
    text = str(message)
    print(text, flush=True)
    with self._log_lock:
      with (self.output_dir / "profile_action.log").open(
          "a", encoding="utf-8", errors="replace") as log:
        log.write(text + "\n")
      if self.summary_path is not None:
        with self.summary_path.open(
            "a", encoding="utf-8", errors="replace") as summary:
          summary.write(text + "\n")


def _validate_wait_argument(name: str, value: float | None,
                            allow_none: bool) -> float | None:
  if value is None and allow_none:
    return None
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise ValueError(f"{name}必须是数字" + ("或 None" if allow_none else ""))
  value = float(value)
  if not value > 0:
    raise ValueError(f"{name}必须大于 0")
  return value


def _match_line(line: str, pattern: str | Pattern[str]) -> str | None:
  if isinstance(pattern, str):
    return pattern if pattern in line else None
  match = pattern.search(line)
  return match.group(0) if match else None


async def _wait_for_app_log(
    context: ProfileActionContext,
    pattern: str | Pattern[str],
    *,
    timeout_seconds: float | None = None,
    include_existing: bool = False,
    poll_interval_seconds: float = 0.2,
) -> AppLogMatch:
  """增量等待本轮目标 PID 的 logcat 行命中指定内容。"""
  if not isinstance(pattern, (str, re.Pattern)):
    raise TypeError("pattern 必须是 str 或已编译正则")
  timeout_seconds = _validate_wait_argument(
      "timeout_seconds", timeout_seconds, allow_none=True)
  poll_interval = _validate_wait_argument(
      "poll_interval_seconds", poll_interval_seconds, allow_none=False)
  assert poll_interval is not None

  path = context.logcat_path
  offset = 0
  if not include_existing and path.exists():
    offset = path.stat().st_size
  tail = b""
  loop = asyncio.get_running_loop()
  deadline = (
      loop.time() + timeout_seconds if timeout_seconds is not None else None)

  while True:
    if deadline is not None and loop.time() >= deadline:
      raise AppLogTimeoutError(
          "APP_LOG_TIMEOUT|"
          f"pattern={pattern!s}|pid={context.pid}|"
          f"timeout_s={timeout_seconds:g}|path={path}"
      )

    if path.exists():
      size = path.stat().st_size
      if size < offset:
        offset = 0
        tail = b""
      if size > offset:
        with path.open("rb") as logcat:
          logcat.seek(offset)
          chunk = logcat.read()
          offset = logcat.tell()
        data = tail + chunk
        complete = data.endswith((b"\n", b"\r"))
        parts = data.splitlines()
        if parts and not complete:
          tail = parts.pop()
        else:
          tail = b""
        for raw_line in parts:
          line = raw_line.decode("utf-8", errors="replace")
          pid_match = LOGCAT_PID_PATTERN.search(line)
          if not pid_match or int(pid_match.group(1)) != context.pid:
            continue
          matched_text = _match_line(line, pattern)
          if matched_text is not None:
            return AppLogMatch(
                line=line, matched_text=matched_text, pid=context.pid)

    sleep_seconds = poll_interval
    if deadline is not None:
      sleep_seconds = min(sleep_seconds, max(0.0, deadline - loop.time()))
    await asyncio.sleep(sleep_seconds)


def current_android_serial() -> str:
  return os.environ.get("ANDROID_SERIAL", "")


def _run_adb_process(
    context: ProfileActionContext,
    *args: str,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
  """执行 ADB 的底层同步实现，运行异常由 Session 转为结果。"""
  command = [context.adb]
  if context.android_serial:
    command.extend(["-s", context.android_serial])
  command.extend(args)
  return subprocess.run(
      command,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      check=False,
      timeout=timeout_seconds,
  )


def _find_poco_port(
    context: ProfileActionContext,
    *,
    min_port: int = 5001,
    max_port: int = 5005,
) -> int:
  """从本轮目标 App 日志中返回最后一次报告的 Poco 监听端口。"""
  if min_port > max_port:
    raise ValueError("min_port不能大于max_port")
  ports: list[int] = []
  with context.logcat_path.open(encoding="utf-8", errors="replace") as logcat:
    for line in logcat:
      pid_match = LOGCAT_PID_PATTERN.search(line)
      port_match = POCO_PORT_PATTERN.search(line)
      if (pid_match and int(pid_match.group(1)) == context.pid and
          port_match):
        ports.append(int(port_match.group(1)))
  if not ports:
    raise RuntimeError(f"poco_port_missing:pid={context.pid}")
  port = ports[-1]
  if not min_port <= port <= max_port:
    raise RuntimeError(f"poco_port_out_of_range:port={port}")
  return port


def _receive_exact(sock: socket.socket, size: int) -> bytes:
  """完整读取 Poco 长度帧，连接提前关闭时输出已接收长度。"""
  chunks: list[bytes] = []
  remaining = size
  while remaining > 0:
    chunk = sock.recv(remaining)
    if not chunk:
      raise ConnectionError(
          f"rpc_response_truncated:expected={size}|received={size - remaining}")
    chunks.append(chunk)
    remaining -= len(chunk)
  return b"".join(chunks)


def _invoke_rpc_transport(
    local_port: int,
    method: str,
    params: list[Any] | dict[str, Any] | None = None,
    *,
    request_id: int | str = 1,
    timeout_seconds: float,
    max_response_bytes: int = 1024 * 1024,
) -> dict[str, Any]:
  """在已建立的转发上执行一次 Poco RPC。"""
  request = {
      "jsonrpc": "2.0",
      "method": method,
      "params": [] if params is None else params,
      "id": request_id,
  }
  body = json.dumps(
      request, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
  with socket.create_connection(
      ("127.0.0.1", local_port), timeout=timeout_seconds) as sock:
    sock.settimeout(timeout_seconds)
    sock.sendall(struct.pack("<I", len(body)) + body)
    response_size = struct.unpack("<I", _receive_exact(sock, 4))[0]
    if response_size <= 0 or response_size > max_response_bytes:
      raise RuntimeError(f"rpc_response_size_invalid:size={response_size}")
    response = json.loads(_receive_exact(sock, response_size).decode("utf-8"))
  if not isinstance(response, dict):
    raise RuntimeError("rpc_response_not_object")
  if "error" in response:
    raise RuntimeError(
        "rpc_error:" + json.dumps(response["error"], ensure_ascii=False))
  return response


class ProfileActionSession:
  """测试脚本的一次性公共会话，统一持有 RPC 状态和诊断逻辑。"""

  def __init__(self, context: ProfileActionContext):
    self.context = context
    self._rpc_lock = asyncio.Lock()
    self._rpc_initialized = False
    self._rpc_forward_active = False
    self._rpc_remote_port = 0
    self._rpc_init_error = ""
    self._rpc_init_stage = ""
    self._blocking_tasks: set[asyncio.Task[Any]] = set()

  def __getattr__(self, name: str) -> Any:
    """代理只读 Context 字段，让测试脚本保持接近配置文件。"""
    return getattr(self.context, name)

  def _log(self, message: str) -> str:
    """诊断写入失败不改变原操作控制流。"""
    try:
      self.context.log(message)
      return ""
    except Exception as exc:  # noqa: BLE001 - 日志失败只能降级到控制台。
      error = f"log_failed:{type(exc).__name__}:{exc}"
      print(f"{message}|{error}", flush=True)
      return error

  async def _run_blocking(self, function, *args, **kwargs):
    """跟踪 to_thread，取消后 close() 仍会等底层调用退出再清理资源。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    self._blocking_tasks.add(task)
    task.add_done_callback(self._blocking_tasks.discard)
    return await asyncio.shield(task)

  async def run_adb(
      self,
      *args: str,
      timeout_seconds: float | None = None,
  ) -> CommandResult:
    """执行 ADB；所有设备或命令运行失败均返回结构化结果。"""
    if timeout_seconds is not None and timeout_seconds <= 0:
      raise ValueError("timeout_seconds必须大于0")
    command_text = " ".join(args)
    try:
      process = await self._run_blocking(
          _run_adb_process, self.context, *args,
          timeout_seconds=timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - 运行失败统一结果化。
      error = f"{type(exc).__name__}:{exc}"
      self._log(
          f"PROFILE_ACTION_ADB=FAIL|command={command_text}|error={error}")
      return CommandResult(False, "adb", error=error)
    if process.returncode != 0:
      error = (
          f"adb_command_failed:rc={process.returncode}|"
          f"stderr={process.stderr.strip()}")
      self._log(
          "PROFILE_ACTION_ADB=FAIL|"
          f"command={command_text}|rc={process.returncode}|error={error}")
      return CommandResult(
          False, "adb", error=error, returncode=process.returncode,
          stdout=process.stdout, stderr=process.stderr)
    self._log(
        f"PROFILE_ACTION_ADB=PASS|command={command_text}|rc=0")
    return CommandResult(
        True, "adb", returncode=process.returncode,
        stdout=process.stdout, stderr=process.stderr)

  async def _initialize_rpc(self) -> None:
    """首次 RPC 时只扫描一次端口并建立一条本轮复用的转发。"""
    async with self._rpc_lock:
      if self._rpc_initialized:
        return
      self._rpc_initialized = True
      local_target = f"tcp:{self.context.rpc_local_port}"
      try:
        self._rpc_init_stage = "find_port"
        self._rpc_remote_port = await self._run_blocking(
            _find_poco_port, self.context)
        self._rpc_init_stage = "remove_stale_forward"
        # 残留转发不存在时 adb 可能返回非零，这一步只做尽力清理。
        await self._run_blocking(
            _run_adb_process, self.context, "forward", "--remove",
            local_target)
        self._rpc_init_stage = "forward"
        process = await self._run_blocking(
            _run_adb_process, self.context, "forward", local_target,
            f"tcp:{self._rpc_remote_port}")
        if process.returncode != 0:
          raise RuntimeError(
              f"adb_forward_failed:rc={process.returncode}|"
              f"stderr={process.stderr.strip()}")
        self._rpc_forward_active = True
        self._rpc_init_stage = "ready"
        self._log(
            "PROFILE_ACTION_RPC_INIT=PASS|"
            f"local_port={self.context.rpc_local_port}|"
            f"remote_port={self._rpc_remote_port}")
      except Exception as exc:  # noqa: BLE001 - 初始化失败由 RPC 结果返回。
        self._rpc_init_error = f"{type(exc).__name__}:{exc}"
        self._log(
            "PROFILE_ACTION_RPC_INIT=FAIL|"
            f"local_port={self.context.rpc_local_port}|"
            f"remote_port={self._rpc_remote_port}|"
            f"stage={self._rpc_init_stage}|error={self._rpc_init_error}")

  def _write_rpc_detail(
      self,
      detail_file: str | Path | None,
      method: str,
      params: list[Any] | dict[str, Any] | None,
      result: RpcResult,
  ) -> str:
    if detail_file is None:
      return ""
    path = (self.context.output_dir / detail_file).resolve()
    output_dir = self.context.output_dir.resolve()
    if path != output_dir and output_dir not in path.parents:
      raise ValueError("detail_file必须位于本轮输出目录内")
    try:
      gm = params[0] if isinstance(params, list) and params else ""
      path.write_text(
          f"method={method}\n"
          f"gm={gm}\n"
          f"params={json.dumps(params, ensure_ascii=False)}\n"
          f"local_port={result.local_port}\n"
          f"remote_port={result.remote_port}\n"
          f"stage={result.stage}\n"
          "response=" + (
              json.dumps(result.response, ensure_ascii=False)
              if result.response is not None else "") + "\n"
          f"error={result.error}\n",
          encoding="utf-8")
      return ""
    except Exception as exc:  # noqa: BLE001 - 详情失败合并进操作结果。
      return f"detail_write_failed:{type(exc).__name__}:{exc}"

  async def invoke_rpc(
      self,
      method: str,
      params: list[Any] | dict[str, Any] | None = None,
      *,
      request_id: int | str = 1,
      timeout_seconds: float | None = None,
      max_response_bytes: int = 1024 * 1024,
      expected_result: Any = True,
      check_result: bool = True,
      detail_file: str | Path | None = "gm_rpc.txt",
  ) -> RpcResult:
    """调用 RPC；运行失败均记录并返回，是否抛异常由测试脚本决定。"""
    if not method:
      raise ValueError("method不能为空")
    if timeout_seconds is None:
      timeout_seconds = float(os.environ.get("HEAP_PROFILE_RPC_TIMEOUT_S", "10"))
    if timeout_seconds <= 0:
      raise ValueError("timeout_seconds必须大于0")
    if max_response_bytes <= 0:
      raise ValueError("max_response_bytes必须大于0")

    await self._initialize_rpc()
    if self._rpc_init_error:
      result = RpcResult(
          False, "rpc", error=self._rpc_init_error,
          local_port=self.context.rpc_local_port,
          remote_port=self._rpc_remote_port, method=method,
          stage=self._rpc_init_stage)
    else:
      try:
        response = await self._run_blocking(
            _invoke_rpc_transport,
            self.context.rpc_local_port,
            method,
            params,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        if check_result:
          actual_result = response.get("result")
          if (type(actual_result) is not type(expected_result) or
              actual_result != expected_result):
            raise RuntimeError(
                "rpc_result_unexpected:"
                f"expected={expected_result!r}|actual={actual_result!r}")
        result = RpcResult(
            True, "rpc", response=response,
            local_port=self.context.rpc_local_port,
            remote_port=self._rpc_remote_port, method=method, stage="response")
      except Exception as exc:  # noqa: BLE001 - RPC 运行失败统一结果化。
        result = RpcResult(
            False, "rpc", error=f"{type(exc).__name__}:{exc}",
            local_port=self.context.rpc_local_port,
            remote_port=self._rpc_remote_port, method=method, stage="response")

    detail_error = self._write_rpc_detail(detail_file, method, params, result)
    if detail_error:
      error = result.error + ("|" if result.error else "") + detail_error
      result = RpcResult(
          False, "rpc", error=error, response=result.response,
          local_port=result.local_port, remote_port=result.remote_port,
          method=result.method, stage=result.stage)
    status = "PASS" if result.success else "FAIL"
    line = (
        f"PROFILE_ACTION_RPC={status}|method={method}|"
        f"local_port={result.local_port}|remote_port={result.remote_port}|"
        f"stage={result.stage}")
    if result.error:
      line += f"|error={result.error}"
    self._log(line)
    return result

  async def wait_for_app_log(
      self,
      pattern: str | Pattern[str],
      *,
      timeout_seconds: float | None = None,
      include_existing: bool = False,
      poll_interval_seconds: float = 0.2,
  ) -> AppLogResult:
    """等待 App 日志；超时等运行失败返回结果，取消保持传播。"""
    try:
      match = await _wait_for_app_log(
          self.context, pattern, timeout_seconds=timeout_seconds,
          include_existing=include_existing,
          poll_interval_seconds=poll_interval_seconds)
    except (TypeError, ValueError):
      raise
    except Exception as exc:  # noqa: BLE001 - CancelledError 不属于 Exception。
      error = f"{type(exc).__name__}:{exc}"
      self._log(
          f"PROFILE_ACTION_APP_LOG=FAIL|pattern={pattern!s}|error={error}")
      return AppLogResult(False, "app_log", error=error)
    self._log(
        f"PROFILE_ACTION_APP_LOG=PASS|pattern={pattern!s}|pid={match.pid}")
    return AppLogResult(True, "app_log", match=match)

  async def wait_forever(self) -> None:
    """保持测试协程运行，直到采集器取消。"""
    await asyncio.Event().wait()

  async def close(self) -> ActionOperationResult:
    """等待底层调用结束并清理公共资源；清理失败只返回结果。"""
    pending = list(self._blocking_tasks)
    if pending:
      await asyncio.gather(*pending, return_exceptions=True)
    may_have_forward = (
        self._rpc_forward_active or
        self._rpc_init_stage in ("remove_stale_forward", "forward", "ready"))
    if not may_have_forward:
      return ActionOperationResult(True, "close")
    local_target = f"tcp:{self.context.rpc_local_port}"
    try:
      process = await self._run_blocking(
          _run_adb_process, self.context, "forward", "--remove", local_target)
      if process.returncode != 0:
        raise RuntimeError(
            f"adb_forward_remove_failed:rc={process.returncode}|"
            f"stderr={process.stderr.strip()}")
    except Exception as exc:  # noqa: BLE001 - 清理失败不能覆盖测试结果。
      error = f"{type(exc).__name__}:{exc}"
      self._log(f"PROFILE_ACTION_CLOSE=FAIL|error={error}")
      return ActionOperationResult(False, "close", error)
    finally:
      self._rpc_forward_active = False
    self._log("PROFILE_ACTION_CLOSE=PASS")
    return ActionOperationResult(True, "close")
