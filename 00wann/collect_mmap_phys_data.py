#!/usr/bin/env python3
"""采集 mmap 物理内存归因所需的数据。

脚本会同时完成三件事：
1. 启动 Perfetto，采集 mmap/munmap/mremap syscall 和 mmap 调用栈。
2. 周期性拉取 /proc/<pid>/smaps，作为真实物理内存 PSS/RSS 快照。
3. 可选调用 mmap_phys_analyzer.py，生成 Perfetto JSON 和 pprof 数据。
"""

import argparse
import csv
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from device_test_framework.actions import (
    ProfileActionContext,
    load_action_module,
    resolve_action_module_path,
    run_profile_action_module,
)

ARM64_MMAP_NR = 222
ARM64_MUNMAP_NR = 215
ARM64_MREMAP_NR = 216

IS_INTERRUPTED = False
SMAPS_HEADER_RE = re.compile(rb"^[0-9a-fA-F]+-[0-9a-fA-F]+\s+")
RPC_LOCAL_PORT = int(os.environ.get("HEAP_PROFILE_RPC_LOCAL_PORT", "12346"))

TRACE_HEALTH_STATS = (
    "traced_buf_buffer_size",
    "traced_buf_bytes_written",
    "traced_buf_bytes_overwritten",
    "traced_buf_chunks_overwritten",
    "traced_buf_chunks_discarded",
    "traced_buf_trace_writer_packet_loss",
    "traced_buf_patches_failed",
    "traced_buf_abi_violations",
    "perf_cpu_lost_records",
    "perf_aux_lost",
    "perf_samples_skipped_dataloss",
    "ftrace_cpu_overrun_delta",
    "ftrace_cpu_commit_overrun_delta",
    "ftrace_cpu_dropped_events_delta",
    "ftrace_cpu_has_data_loss",
    "ftrace_cpu_read_events_delta",
)


def on_signal(_sig, _frame):
  global IS_INTERRUPTED
  IS_INTERRUPTED = True


def run(args, **kwargs):
  print("+ " + " ".join(args))
  return subprocess.run(args, **kwargs)


def check_output(args, log: bool = True, **kwargs) -> str:
  if log:
    print("+ " + " ".join(args))
  return subprocess.check_output(args, **kwargs).decode(
      "utf-8", errors="replace")


def adb_shell(cmd: str, log: bool = True) -> str:
  return check_output(["adb", "shell", cmd], log=log,
                      stderr=subprocess.STDOUT).strip()


def format_wait_status(name: str, elapsed_s: float) -> str:
  return f"等待应用启动: {name} 已等待 {elapsed_s:.1f}s"


def format_smaps_progress_line(path: str, size_bytes: int,
                               remaining_s: float) -> str:
  return f"smaps 快照: {path} ({size_bytes} bytes) 剩余: {max(0.0, remaining_s):.1f}s"


def resolve_launch_activity(name: str) -> Optional[str]:
  out = adb_shell(f"cmd package resolve-activity --brief {shell_quote(name)}")
  for line in reversed(out.splitlines()):
    value = line.strip()
    if "/" in value and not value.startswith("priority="):
      return value
  return None


def launch_app(name: str):
  """目标进程未运行时，通过 launcher Activity 固定启动。"""
  activity = os.environ.get("MMAP_PHYS_ACTIVITY", "").strip()
  if activity:
    print(f"目标进程未启动，使用 am start 启动 Activity: {activity}")
    adb_shell(f"am start -n {shell_quote(activity)}")
    return
  activity = resolve_launch_activity(name)
  if not activity:
    raise RuntimeError(
        f"目标进程未启动，且无法解析 launcher Activity: {name}。"
        "请设置 MMAP_PHYS_ACTIVITY，或在手机上手动启动目标场景后重试。")
  print(f"目标进程未启动，使用 am start 启动 Activity: {activity}")
  adb_shell(f"am start -n {shell_quote(activity)}")


def force_stop_app(name: str, timeout_s: float = 5.0):
  """验证 attempt 前停止目标应用，保证下一轮重新启动并获取新 pid。"""
  print(f"重启验证目标应用，先停止进程: {name}")
  adb_shell(f"am force-stop {shell_quote(name)}")
  deadline = time.time() + timeout_s
  while time.time() < deadline:
    out = adb_shell(f"pidof {shell_quote(name)} || true", log=False).strip()
    if not out:
      return
    time.sleep(0.2)
  print(f"WARN: force-stop 后目标进程仍存在，将继续尝试重新采集: {name}", file=sys.stderr)


class TwoLineProgress:
  """用两行原地刷新高频采样状态，避免长时间采集刷屏。"""

  def __init__(self, stream=sys.stdout):
    self.stream = stream
    self.started = False

  def update(self, line1: str, line2: str):
    if self.started:
      print("\033[2F", end="", file=self.stream)
    print("\r\033[K" + line1, file=self.stream)
    print("\r\033[K" + line2, file=self.stream, flush=True)
    self.started = True


def wait_for_pid(name: str, timeout_s: int) -> int:
  start_time = time.time()
  deadline = time.time() + timeout_s if timeout_s > 0 else None
  launch_attempted = False
  while True:
    elapsed_s = time.time() - start_time
    out = adb_shell(f"pidof {shell_quote(name)} || true", log=False).strip()
    if out:
      pid = int(out.split()[0])
      print(f"\r\033[K应用已启动: {name} pid={pid} 等待 {elapsed_s:.1f}s")
      return pid
    if deadline is not None and time.time() > deadline:
      raise RuntimeError(f"等待目标进程超时: {name}")
    if not launch_attempted:
      launch_app(name)
      launch_attempted = True
    print("\r\033[K" + format_wait_status(name, elapsed_s), end="", flush=True)
    time.sleep(0.2)


def shell_quote(value: str) -> str:
  return "'" + value.replace("'", "'\\''") + "'"


def build_perfetto_config(name: str,
                          duration_ms: int,
                          buffer_kb: int,
                          include_ftrace: bool,
                          kernel_frames: bool,
                          perf_ring_buffer_pages: int = 32768,
                          perf_ring_buffer_read_period_ms: int = 25,
                          include_mmap_callstacks: bool = True) -> str:
  kernel_frames_value = "true" if kernel_frames else "false"
  perf_ring_buffer_block = ""
  if perf_ring_buffer_pages > 0:
    perf_ring_buffer_block += f"      ring_buffer_pages: {perf_ring_buffer_pages}\n"
  if perf_ring_buffer_read_period_ms > 0:
    perf_ring_buffer_block += (
        f"      ring_buffer_read_period_ms: {perf_ring_buffer_read_period_ms}\n"
    )
  ftrace_block = ""
  if include_ftrace:
    # 部分设备没有 per-syscall tracefs 目录，syscall_events 会产出 0 个事件；
    # 因此验证模式也保留 raw_syscalls 作为 mmap/munmap/mremap 的兼容兜底。
    high_volume_events = ""
    if include_mmap_callstacks:
      high_volume_events = """      ftrace_events: "raw_syscalls/sys_enter"
      ftrace_events: "raw_syscalls/sys_exit"
      ftrace_events: "sched/sched_switch"
"""
    else:
      high_volume_events = """      ftrace_events: "raw_syscalls/sys_enter"
      ftrace_events: "raw_syscalls/sys_exit"
      ftrace_events: "sched/sched_switch"
"""
    ftrace_block = """
data_sources {
  config {
    name: "linux.ftrace"
    ftrace_config {
      syscall_events: "sys_mmap"
      syscall_events: "sys_munmap"
      syscall_events: "sys_mremap"
      syscall_events: "sys_madvise"
%s    }
  }
}
""" % high_volume_events
  process_stats_block = ""
  if include_ftrace:
    process_stats_block = """
data_sources {
  config {
    name: "linux.process_stats"
    process_stats_config {
      scan_all_processes_on_start: true
    }
  }
}
"""
  perf_block = ""
  if include_mmap_callstacks:
    perf_block = f"""
data_sources {{
  config {{
    name: "linux.perf"
    perf_event_config {{
      timebase {{
        period: 1
        tracepoint {{
          name: "raw_syscalls:sys_enter"
          filter: "id == {ARM64_MMAP_NR}"
        }}
      }}
      callstack_sampling {{
        scope {{
          target_cmdline: "{name}"
        }}
        user_frames: UNWIND_DWARF
        kernel_frames: {kernel_frames_value}
      }}
{perf_ring_buffer_block.rstrip()}
    }}
  }}
}}
"""

  duration_line = f"duration_ms: {duration_ms}\n" if duration_ms > 0 else ""
  return f"""buffers {{
  size_kb: {buffer_kb}
  fill_policy: RING_BUFFER
}}

{ftrace_block}
{process_stats_block}
{perf_block}

{duration_line.rstrip()}
write_into_file: true
flush_period_ms: 5000
flush_timeout_ms: 30000
"""


def write_config(config: str,
                 output_dir: str,
                 filename: str = "mmap_phys_config.pbtxt") -> str:
  host_path = os.path.join(output_dir, filename)
  with open(host_path, "w", encoding="utf-8") as fd:
    fd.write(config)
  print(f"Perfetto 配置已保存: {host_path}")
  return host_path


def start_perfetto(config: str, device_trace: str, no_guardrails: bool) -> int:
  cmd = [
      "adb", "shell", "perfetto", "--txt", "-c", "-", "-o", device_trace, "-d"
  ]
  if no_guardrails:
    cmd.append("--no-guardrails")
  print("+ " + " ".join(cmd) + " < mmap_phys_config.pbtxt")
  try:
    out = check_output(
        cmd, log=False, input=config.encode("utf-8"), stderr=subprocess.STDOUT)
  except subprocess.CalledProcessError as exc:
    output = exc.output.decode("utf-8", errors="replace") if exc.output else ""
    raise RuntimeError(
        f"启动 perfetto 失败，退出码: {exc.returncode}，输出: {output}") from exc
  try:
    pid = int(out.strip().splitlines()[-1])
  except (ValueError, IndexError) as exc:
    raise RuntimeError(f"启动 perfetto 失败，输出: {out}") from exc
  print(f"Perfetto 已启动: pid={pid}")
  return pid


@dataclass(frozen=True)
class RootTracedPerfState:
  """记录 root producer 及启动前系统状态，供异常路径精确恢复。"""

  pid: int
  original_perf_lsm_hooks: str
  original_service_state: str


def _run_device_adb(args: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
  """执行固定真机 adb 命令，并保留输出用于阶段校验。"""
  return subprocess.run(
      _device_adb_command() + args,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      check=False,
      timeout=timeout)


def start_root_traced_perf() -> Optional[RootTracedPerfState]:
  """在 Perfetto 会话前启动唯一 root producer，并阻止 init 重复拉起。"""
  enabled = os.environ.get("MMAP_PHYS_USE_ROOT_TRACED_PERF", "0").strip()
  if enabled.lower() in ("0", "false", "no", "off"):
    print("跳过 root traced_perf：MMAP_PHYS_USE_ROOT_TRACED_PERF=0")
    return None

  hooks = _run_device_adb(
      ["shell", "getprop", "sys.init.perf_lsm_hooks"])
  service = _run_device_adb(
      ["shell", "getprop", "init.svc.traced_perf"])
  if hooks.returncode != 0 or service.returncode != 0:
    raise RuntimeError("读取 traced_perf 原始系统状态失败")
  original_hooks = hooks.stdout.strip()
  original_service = service.stdout.strip()

  # traced_perf.rc 会在 linux.perf 数据源启动时根据 lazy 属性再次拉起 nobody
  # producer。先临时关闭对应 init 条件，保证本轮只有 root producer 注册。
  commands = (
      ["shell", "su", "0", "setprop", "sys.init.perf_lsm_hooks", "0"],
      ["shell", "su", "0", "setprop", "ctl.stop", "traced_perf"],
      ["shell", "su", "0", "killall", "traced_perf"],
  )
  for command in commands:
    print("+ " + " ".join(_device_adb_command() + command))
    _run_device_adb(command)

  command = ["shell", "su", "0", "/system/bin/traced_perf", "--background"]
  print("+ " + " ".join(_device_adb_command() + command))
  try:
    proc = _run_device_adb(command, timeout=15)
  except (OSError, subprocess.SubprocessError) as exc:
    _restore_traced_perf_system_state(original_hooks, original_service)
    raise RuntimeError(f"root traced_perf 启动异常: {exc}") from exc
  if proc.returncode != 0:
    _restore_traced_perf_system_state(original_hooks, original_service)
    raise RuntimeError(
        f"root traced_perf 启动失败，退出码={proc.returncode}|输出={proc.stdout.strip()}")
  lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
  pid = int(lines[-1]) if lines and lines[-1].isdigit() else None
  if pid is None:
    _restore_traced_perf_system_state(original_hooks, original_service)
    raise RuntimeError(
        f"root traced_perf 启动后未找到 PID|输出={proc.stdout.strip()}|"
        "请检查 /system/bin/traced_perf 是否支持 --background")

  time.sleep(0.5)
  processes = _run_device_adb(
      ["shell", "ps", "-A", "-o", "USER,PID,NAME"])
  producer_rows = [
      line.split() for line in processes.stdout.splitlines()
      if line.split() and line.split()[-1] == "traced_perf"
  ]
  valid = (processes.returncode == 0 and len(producer_rows) == 1 and
           len(producer_rows[0]) >= 3 and producer_rows[0][0] == "root" and
           producer_rows[0][1] == str(pid))
  if not valid:
    _run_device_adb(["shell", "su", "0", "kill", str(pid)])
    _restore_traced_perf_system_state(original_hooks, original_service)
    rows = " ; ".join(" ".join(row) for row in producer_rows) or "<empty>"
    raise RuntimeError(f"root traced_perf 唯一性检查失败|processes={rows}")

  print(
      "root traced_perf 已就绪: "
      f"pid={pid}|producer_count=1|lazy_init_suppressed=1")
  return RootTracedPerfState(pid, original_hooks, original_service)


def _restore_traced_perf_system_state(original_hooks: str,
                                       original_service: str) -> None:
  """恢复启动前的 init 属性和 traced_perf service 状态。"""
  _run_device_adb([
      "shell", "su", "0", "setprop", "sys.init.perf_lsm_hooks",
      original_hooks
  ])
  action = "ctl.start" if original_service == "running" else "ctl.stop"
  _run_device_adb(
      ["shell", "su", "0", "setprop", action, "traced_perf"])


def stop_root_traced_perf(state: Optional[RootTracedPerfState]) -> None:
  """停止本轮 root producer，并恢复采集前的 init producer 状态。"""
  if state is None:
    return
  proc = _run_device_adb(
      ["shell", "su", "0", "kill", str(state.pid)])
  if proc.returncode != 0:
    print(f"WARN: root traced_perf 停止失败 pid={state.pid}: {proc.stdout.strip()}",
          file=sys.stderr)
  _restore_traced_perf_system_state(
      state.original_perf_lsm_hooks, state.original_service_state)
  print(
      "traced_perf 系统状态已恢复: "
      f"perf_lsm_hooks={state.original_perf_lsm_hooks or '<empty>'}|"
      f"service={state.original_service_state or '<empty>'}")


def _device_adb_command() -> list[str]:
  """生成带固定真机序列号的 adb 前缀，避免多设备时误选目标。"""
  command = ["adb"]
  serial = os.environ.get("ANDROID_SERIAL", "").strip()
  if serial:
    command.extend(["-s", serial])
  return command


def device_time_ns(log: bool = True) -> int:
  uptime = adb_shell("cat /proc/uptime", log=log).split()[0]
  return int(float(uptime) * 1_000_000_000)


def is_process_alive(pid: int) -> bool:
  return subprocess.call(["adb", "shell", f"[ -d /proc/{pid} ]"],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL) == 0


def is_valid_smaps(data: bytes) -> bool:
  for line in data.splitlines():
    if line.strip():
      return bool(SMAPS_HEADER_RE.match(line))
  return False


def read_smaps_with_mode(pid: int, use_su: bool) -> bytes:
  if use_su:
    cmd = ["adb", "exec-out", "su", "0", "cat", f"/proc/{pid}/smaps"]
  else:
    cmd = ["adb", "exec-out", "cat", f"/proc/{pid}/smaps"]
  return subprocess.check_output(cmd, stderr=subprocess.STDOUT)


def read_smaps_with_run_as(pid: int, package: str) -> bytes:
  cmd = ["adb", "exec-out", "run-as", package, "cat", f"/proc/{pid}/smaps"]
  return subprocess.check_output(cmd, stderr=subprocess.STDOUT)


def read_smaps(pid: int,
               use_su: bool,
               run_as_package: Optional[str] = None) -> bytes:
  data = read_smaps_with_mode(pid, use_su)
  if is_valid_smaps(data):
    return data
  if not use_su:
    if run_as_package:
      print(f"普通权限读取 smaps 无效，自动尝试 run-as {run_as_package}", file=sys.stderr)
      data = read_smaps_with_run_as(pid, run_as_package)
      if is_valid_smaps(data):
        return data
    print("普通权限读取 smaps 无效，自动尝试 su 0", file=sys.stderr)
    data = read_smaps_with_mode(pid, True)
    if is_valid_smaps(data):
      return data
  preview = data.decode("utf-8", errors="replace").strip().splitlines()
  raise RuntimeError("读取到的 smaps 不是有效 VMA 内容: " +
                     (preview[0] if preview else "<empty>"))


def collect_smaps(pid: int,
                  perfetto_pid: int,
                  smaps_dir: str,
                  interval_ms: int,
                  use_su: bool,
                  duration_ms: int,
                  run_as_package: Optional[str] = None,
                  stop_event: Optional[threading.Event] = None):
  os.makedirs(smaps_dir, exist_ok=True)
  interval_s = interval_ms / 1000.0
  duration_s = duration_ms / 1000.0
  start_s = time.monotonic()
  sample_count = 0
  progress = TwoLineProgress()
  while (not IS_INTERRUPTED and is_process_alive(perfetto_pid) and
         not (stop_event and stop_event.is_set())):
    ts_ns = device_time_ns(log=False)
    path = os.path.join(smaps_dir, f"{ts_ns}.smaps")
    try:
      data = read_smaps(pid, use_su, run_as_package=run_as_package)
      with open(path, "wb") as fd:
        fd.write(data)
      sample_count += 1
      remaining_s = duration_s - (time.monotonic() - start_s)
      progress_text = (
          format_smaps_progress_line(path, len(data), remaining_s)
          if duration_ms > 0 else
          f"smaps 快照: {path} ({len(data)} bytes) 等待测试操作结束")
      progress.update("+ adb shell cat /proc/uptime",
                      progress_text)
    except subprocess.CalledProcessError as exc:
      print(
          f"读取 smaps 失败: {exc.output.decode('utf-8', errors='replace')}",
          file=sys.stderr)
    except RuntimeError as exc:
      print(f"读取 smaps 失败: {exc}", file=sys.stderr)
    if stop_event:
      stop_event.wait(interval_s)
    else:
      time.sleep(interval_s)
  print(f"smaps 采样结束: {sample_count} 个快照")
  if sample_count == 0:
    raise RuntimeError("没有采集到有效 smaps 快照")


def stop_perfetto(perfetto_pid: int):
  if is_process_alive(perfetto_pid):
    print(f"停止 Perfetto: pid={perfetto_pid}")
    subprocess.call(["adb", "shell", "kill", "-INT", str(perfetto_pid)])
  while is_process_alive(perfetto_pid):
    time.sleep(0.2)


def start_logcat_capture(output_dir: str):
  """清空旧日志并持续保存本轮 logcat。"""
  subprocess.call(["adb", "logcat", "-c"])
  stdout_file = open(
      os.path.join(output_dir, "logcat.txt"), "wb")
  stderr_file = open(
      os.path.join(output_dir, "logcat.err.txt"), "wb")
  process = subprocess.Popen(
      ["adb", "logcat", "-v", "time"],
      stdout=stdout_file,
      stderr=stderr_file)
  return process, stdout_file, stderr_file


def stop_logcat_capture(process, stdout_file, stderr_file):
  if process and process.poll() is None:
    process.terminate()
    try:
      process.wait(timeout=5)
    except subprocess.TimeoutExpired:
      process.kill()
      process.wait(timeout=5)
  for file_obj in (stdout_file, stderr_file):
    if file_obj:
      file_obj.close()


def pull_trace(device_trace: str, host_trace: str):
  subprocess.check_call(["adb", "pull", device_trace, host_trace])
  subprocess.call(["adb", "shell", "rm", "-f", device_trace])
  print(f"trace 已保存: {host_trace}")


def symbolize_trace(traceconv: Optional[str], trace_path: str,
                    output_dir: str) -> str:
  """按 heap_profile.py 的方式把 traceconv 符号包拼回 trace。"""
  if not traceconv:
    print("跳过 trace 符号化：未指定 --traceconv", file=sys.stderr)
    return trace_path
  binary_path = os.getenv("PERFETTO_BINARY_PATH")
  if not binary_path:
    print("跳过 trace 符号化：未设置 PERFETTO_BINARY_PATH，"
          "libil2cpp.so 等业务 so 无法自动解析",
          file=sys.stderr)
    return trace_path

  symbols_path = os.path.join(output_dir, "symbols")
  symbolized_path = os.path.join(output_dir, "symbolized-trace")
  print("+ " + " ".join([traceconv, "symbolize", trace_path]) +
        f" > {symbols_path}")
  with open(symbols_path, "wb") as symbols:
    ret = subprocess.call([traceconv, "symbolize", trace_path],
                          env=dict(
                              os.environ, PERFETTO_BINARY_PATH=binary_path),
                          stdout=symbols)
  if ret != 0:
    print(
        f"WARN: traceconv symbolize 失败，退出码={ret}，继续使用原始 trace", file=sys.stderr)
    return trace_path

  # Perfetto 的符号包是追加 packet；与 raw trace 拼接后 trace_processor 可直接读取。
  with open(symbolized_path, "wb") as output:
    for path in (trace_path, symbols_path):
      with open(path, "rb") as source:
        while True:
          chunk = source.read(1024 * 1024)
          if not chunk:
            break
          output.write(chunk)
  print(f"符号化 trace 已保存: {symbolized_path}")
  return symbolized_path


def parse_trace_processor_csv(output: str,
                              required_columns=("name", "idx", "value")):
  """从 trace_processor 日志混合输出里截取 CSV 查询结果。"""
  # trace_processor 的耗时日志有时会直接贴在 CSV 最后一行后面，先按日志前缀截断。
  lines = [
      re.sub(r"\[\d+\.\d+\]\s+.*$", "", line) for line in output.splitlines()
  ]
  header_index = None
  for i, line in enumerate(lines):
    try:
      header = next(csv.reader([line]))
    except csv.Error:
      continue
    if required_columns is None:
      if len(header) > 1:
        header_index = i
        break
    elif all(column in header for column in required_columns):
      header_index = i
      break
  if header_index is None:
    return []
  return list(csv.DictReader(io.StringIO("\n".join(lines[header_index:]))))


def query_trace_health(trace_processor: str, trace_path: str):
  quoted_names = ", ".join(f"'{name}'" for name in TRACE_HEALTH_STATS)
  sql = ("select name, idx, value from stats "
         f"where name in ({quoted_names}) "
         "order by name, idx")
  print("+ " + " ".join([trace_processor, "query", trace_path, sql]))
  proc = subprocess.run([trace_processor, "query", trace_path, sql],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        check=False)
  if proc.returncode != 0:
    print("Perfetto buffer 健康检查失败:", file=sys.stderr)
    print(proc.stdout, file=sys.stderr)
    return []
  return parse_trace_processor_csv(proc.stdout)


def query_target_perf_callstacks(trace_processor: str, trace_path: str,
                                 pid: int):
  """统计目标进程 perf 样本和已展开调用栈，防止空归因误报成功。"""
  sql = f"""
select
  count(1) as perf_samples,
  count(ps.callsite_id) as perf_callsites
from perf_sample ps
join thread t using (utid)
join process p using (upid)
where p.pid = {int(pid)}
"""
  print("+ " + " ".join([trace_processor, "query", trace_path, sql]))
  proc = subprocess.run([trace_processor, "query", trace_path, sql],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        check=False)
  if proc.returncode != 0:
    print("目标进程 perf 调用栈查询失败:", file=sys.stderr)
    print(proc.stdout, file=sys.stderr)
    return None
  rows = parse_trace_processor_csv(
      proc.stdout, required_columns=("perf_samples", "perf_callsites"))
  if not rows:
    return None
  return {
      "perf_samples": int_value(rows[-1].get("perf_samples")),
      "perf_callsites": int_value(rows[-1].get("perf_callsites")),
  }


def summarize_trace_health(rows):

  def sum_stats(names):
    return sum(
        int(row["value"])
        for row in rows
        if row.get("name") in names and row.get("value") not in ("", None))

  def max_stat(name):
    values = [
        int(row["value"])
        for row in rows
        if row.get("name") == name and row.get("value") not in ("", None)
    ]
    return max(values) if values else 0

  # 分开统计三类 buffer，便于定位应该调哪个配置项。
  return {
      "buffer_size_bytes":
          max_stat("traced_buf_buffer_size"),
      "bytes_written":
          max_stat("traced_buf_bytes_written"),
      "perfetto_data_loss":
          sum_stats({
              "traced_buf_bytes_overwritten",
              "traced_buf_chunks_overwritten",
              "traced_buf_chunks_discarded",
              "traced_buf_trace_writer_packet_loss",
              "traced_buf_patches_failed",
              "traced_buf_abi_violations",
          }),
      "perf_data_loss":
          sum_stats({
              "perf_cpu_lost_records",
              "perf_aux_lost",
          }),
      "perf_samples_skipped_dataloss":
          sum_stats({
              "perf_samples_skipped_dataloss",
          }),
      "ftrace_data_loss":
          sum_stats({
              "ftrace_cpu_overrun_delta",
              "ftrace_cpu_commit_overrun_delta",
              "ftrace_cpu_dropped_events_delta",
              "ftrace_cpu_has_data_loss",
          }),
      "ftrace_read_events":
          sum_stats({
              "ftrace_cpu_read_events_delta",
          }),
  }


def format_mib(value: int) -> str:
  return f"{value / 1024 / 1024:.1f} MiB"


def print_trace_health(summary):
  print("Perfetto buffer 健康检查:")
  print(f"  顶层 trace buffer: size={format_mib(summary['buffer_size_bytes'])}, "
        f"bytes_written={format_mib(summary['bytes_written'])}")
  if summary["perfetto_data_loss"] == 0:
    print("  OK: Perfetto 顶层 ring buffer 未报告覆盖或 packet loss")
  else:
    print(f"  WARN: Perfetto 顶层 ring buffer 丢数计数="
          f"{summary['perfetto_data_loss']}，建议增大 --buffer-kb")
  if summary["ftrace_data_loss"] == 0:
    print("  OK: ftrace 内核 buffer 未报告 overrun/drop")
  else:
    print(f"  WARN: ftrace 内核 buffer 丢数计数={summary['ftrace_data_loss']}，"
          "建议减少 ftrace 事件或增大 ftrace_config.buffer_size_kb")
  if summary["perf_data_loss"] == 0:
    print("  OK: linux.perf 每 CPU ring buffer 未报告 lost records")
  else:
    print(f"  WARN: linux.perf 每 CPU ring buffer 丢样本计数="
          f"{summary['perf_data_loss']}，建议增大 --perf-ring-buffer-pages "
          "或降低采样压力")
  perf_samples_skipped = int(summary.get("perf_samples_skipped_dataloss", 0))
  if perf_samples_skipped == 0:
    print("  OK: traced_perf 内部未报告调用栈 sample 丢失")
  else:
    print(f"  WARN: traced_perf 内部调用栈 sample 丢失计数="
          f"{perf_samples_skipped}，通常表示 reader 到 "
          "unwinder 队列 load shedding，可降低采样压力或减少展开成本")
  if "perf_samples" in summary:
    print(
        "  目标进程调用栈: "
        f"samples={summary['perf_samples']}, "
        f"callsites={summary.get('perf_callsites', 0)}")


def check_trace_health(args, trace_path: str, pid: Optional[int] = None):
  if not args.trace_processor:
    print("跳过 Perfetto buffer 健康检查：未指定 --trace-processor", file=sys.stderr)
    return
  rows = query_trace_health(args.trace_processor, trace_path)
  if not rows:
    print("跳过 Perfetto buffer 健康检查：stats 查询无结果", file=sys.stderr)
    return None
  summary = summarize_trace_health(rows)
  if pid is not None:
    callstack_health = query_target_perf_callstacks(
        args.trace_processor, trace_path, pid)
    if callstack_health:
      summary.update(callstack_health)
  print_trace_health(summary)
  return summary


def int_value(value) -> int:
  if value in ("", None):
    return 0
  return int(float(value))


def parse_meminfo_summary(text: str):
  """解析 dumpsys meminfo 的总 PSS 与 Native Heap Alloc，单位转成 bytes。"""
  summary = {
      "native_heap_pss_bytes": 0,
      "native_heap_alloc_bytes": 0,
      "total_pss_bytes": 0,
  }

  def numbers_in(line: str):
    return [
        int(item.replace(",", "")) for item in re.findall(r"-?[\d,]+", line)
    ]

  for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line:
      continue
    numbers = numbers_in(line)
    if line.startswith("Native Heap") and numbers:
      summary["native_heap_pss_bytes"] = numbers[0] * 1024
      if len(numbers) >= 3:
        summary["native_heap_alloc_bytes"] = numbers[-2] * 1024
    elif line.startswith("TOTAL PSS:") and numbers:
      summary["total_pss_bytes"] = numbers[0] * 1024
    elif re.match(r"^TOTAL\s+-?[\d,]+", line) and numbers:
      summary["total_pss_bytes"] = numbers[0] * 1024
  return summary


def parse_meminfo_table_rows(text: str):
  """解析 dumpsys meminfo 主表行；只取能和 smaps 对账的基础列。"""
  rows = {}
  for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line or ":" in line:
      continue
    match = re.search(r"-?[\d,]+", line)
    if not match:
      continue
    name = line[:match.start()].strip()
    if not name or set(name) <= {"-"}:
      continue
    if name not in MEMINFO_MAIN_TABLE_ROW_NAMES:
      continue
    numbers = [
        int(item.replace(",", "")) for item in re.findall(r"-?[\d,]+", line)
    ]
    if not numbers:
      continue
    item = {
        "pss_bytes": numbers[0] * 1024,
    }
    if len(numbers) >= 5:
      item["rss_bytes"] = numbers[4] * 1024
    if name == "Native Heap" and len(numbers) >= 3:
      item["heap_alloc_bytes"] = numbers[-2] * 1024
    rows[name] = item
  return rows


def capture_meminfo(name: str, output_dir: str) -> str:
  meminfo = adb_shell(f"dumpsys meminfo {shell_quote(name)}")
  path = os.path.join(output_dir, "dumpsys_meminfo.txt")
  with open(path, "w", encoding="utf-8") as fd:
    fd.write(meminfo)
    if not meminfo.endswith("\n"):
      fd.write("\n")
  print(f"dumpsys meminfo 已保存: {path}")
  return path


def query_mmap_validation_syscalls(trace_processor: str, trace_path: str,
                                   pid: int):
  """只查询目标进程 mmap/munmap/mremap syscall，不读取调用栈表。"""
  import mmap_phys_analyzer as analyzer

  sql = build_mmap_validation_syscalls_sql(pid)
  print("+ " + " ".join([trace_processor, "query", trace_path, sql]))
  proc = subprocess.run([trace_processor, "query", trace_path, sql],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        check=False)
  if proc.returncode != 0:
    raise RuntimeError("查询 mmap syscall 失败:\n" + proc.stdout)
  rows = parse_trace_processor_csv(
      proc.stdout,
      required_columns=("event_id", "ts", "utid", "event_name", "arg_id", "key",
                        "int_value", "string_value", "value_type"))
  return build_syscall_events_from_rows(rows, analyzer)


def build_mmap_validation_ctes(pid: int) -> str:
  """构造 mmap 验证共用 CTE；先按 pid/事件名缩小 ftrace，再查 args。"""
  return f"""
target_ftrace_events AS (
  SELECT
    fe.id AS event_id,
    fe.ts AS ts,
    fe.utid AS utid,
    fe.name AS event_name,
    fe.arg_set_id AS arg_set_id
  FROM __intrinsic_ftrace_event fe
  JOIN __intrinsic_thread th ON fe.utid = th.id
  LEFT JOIN __intrinsic_process pr ON th.upid = pr.id
  WHERE (pr.pid = {pid} OR th.tid = {pid})
    AND (
      fe.name LIKE '%mmap%' OR
      fe.name LIKE '%munmap%' OR
      fe.name LIKE '%mremap%' OR
      fe.name LIKE '%sys_enter%' OR
      fe.name LIKE '%sys_exit%'
    )
),
named_syscall_events AS (
  SELECT event_id
  FROM target_ftrace_events
  WHERE event_name LIKE '%mmap%'
     OR event_name LIKE '%munmap%'
     OR event_name LIKE '%mremap%'
),
raw_syscall_events AS (
  SELECT DISTINCT tfe.event_id
  FROM target_ftrace_events tfe
  JOIN __intrinsic_args a ON tfe.arg_set_id = a.arg_set_id
  WHERE (tfe.event_name LIKE '%sys_enter%' OR tfe.event_name LIKE '%sys_exit%')
    AND a.key IN ('id', 'syscall_nr', 'nr')
    AND a.int_value IN ({ARM64_MMAP_NR}, {ARM64_MUNMAP_NR}, {ARM64_MREMAP_NR})
),
interesting_syscall_events AS (
  SELECT event_id FROM named_syscall_events
  UNION
  SELECT event_id FROM raw_syscall_events
),
syscall_rows AS (
  SELECT
    tfe.event_id AS event_id,
    tfe.ts AS ts,
    tfe.utid AS utid,
    tfe.event_name AS event_name,
    a.id AS arg_id,
    a.key AS key,
    IFNULL(a.int_value, 0) AS int_value,
    IFNULL(a.string_value, '') AS string_value,
    a.value_type AS value_type
  FROM target_ftrace_events tfe
  JOIN interesting_syscall_events ise ON tfe.event_id = ise.event_id
  JOIN __intrinsic_args a ON tfe.arg_set_id = a.arg_set_id
)
"""


def build_mmap_validation_syscalls_sql(pid: int) -> str:
  """生成只输出 mmap/munmap/mremap syscall 参数行的 SQL。"""
  return f"""
WITH {build_mmap_validation_ctes(pid)}
SELECT
  event_id,
  ts,
  utid,
  event_name,
  arg_id,
  key,
  int_value,
  string_value,
  value_type
FROM syscall_rows
ORDER BY ts, event_id, arg_id
"""


def build_syscall_events_from_rows(rows, analyzer):
  """把 trace_processor 的 syscall 参数行还原成 SyscallEvent。"""
  grouped = {}
  repeated_arg_count = {}
  for row in rows:
    event_id = int(row["event_id"])
    ev = grouped.get(event_id)
    if ev is None:
      ev = analyzer.SyscallEvent(
          ts=int(row["ts"]),
          utid=int(row["utid"]),
          tid=int(row.get("tid") or row["utid"]),
          name=row["event_name"],
          syscall_id=None,
          ret=None,
          args={})
      grouped[event_id] = ev

    value = analyzer.int_or_none(row.get("int_value"))
    if value is None:
      value = analyzer.int_or_none(row.get("string_value"))
    if value is None:
      continue

    norm = analyzer.normalize_arg_key(row["key"])
    if norm == "args":
      index = repeated_arg_count.get(event_id, 0)
      repeated_arg_count[event_id] = index + 1
      norm = f"arg{index}"
    ev.args[norm] = value
    if norm in ("id", "syscall_nr", "nr"):
      ev.syscall_id = value
    if norm in ("ret", "retval", "return_value"):
      ev.ret = value

  return sorted(grouped.values(), key=lambda event: event.ts)


def build_memory_validation_inputs_sql(pid: int) -> str:
  """一次 trace_processor 查询拿齐健康检查和 mmap syscall。"""
  quoted_names = ", ".join(f"'{name}'" for name in TRACE_HEALTH_STATS)
  return f"""
WITH
{build_mmap_validation_ctes(pid)}
SELECT section, c0, c1, c2, c3, c4, c5, c6, c7, c8, c9
FROM (
  SELECT
    0 AS sort_section,
    0 AS sort_ts,
    0 AS sort_id,
    0 AS sort_arg,
    'health' AS section,
    name AS c0,
    idx AS c1,
    value AS c2,
    '' AS c3,
    '' AS c4,
    '' AS c5,
    '' AS c6,
    '' AS c7,
    '' AS c8,
    '' AS c9
  FROM stats
  WHERE name IN ({quoted_names})
  UNION ALL
  SELECT
    1 AS sort_section,
    ts AS sort_ts,
    event_id AS sort_id,
    arg_id AS sort_arg,
    'syscall' AS section,
    event_id AS c0,
    ts AS c1,
    utid AS c2,
    event_name AS c3,
    arg_id AS c4,
    key AS c5,
    int_value AS c6,
    string_value AS c7,
    value_type AS c8,
    '' AS c9
  FROM syscall_rows
)
ORDER BY sort_section, sort_ts, sort_id, sort_arg
"""


def query_memory_validation_inputs(trace_processor: str, trace_path: str,
                                   pid: int):
  """一次冷加载 trace，拿齐无栈 mmap 健康检查输入。"""
  import mmap_phys_analyzer as analyzer

  sql = build_memory_validation_inputs_sql(pid)
  print("+ " + " ".join([trace_processor, "query", trace_path, sql]))
  proc = subprocess.run([trace_processor, "query", trace_path, sql],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        check=False)
  if proc.returncode != 0:
    raise RuntimeError("查询内存验证输入失败:\n" + proc.stdout)
  rows = parse_trace_processor_csv(
      proc.stdout,
      required_columns=("section", "c0", "c1", "c2", "c3", "c4", "c5", "c6",
                        "c7", "c8", "c9"))
  health_rows = [{
      "name": row.get("c0", ""),
      "idx": row.get("c1", ""),
      "value": row.get("c2", "")
  } for row in rows if row.get("section") == "health"]
  syscall_rows = [{
      "event_id": row.get("c0", ""),
      "ts": row.get("c1", ""),
      "utid": row.get("c2", ""),
      "event_name": row.get("c3", ""),
      "arg_id": row.get("c4", ""),
      "key": row.get("c5", ""),
      "int_value": row.get("c6", ""),
      "string_value": row.get("c7", ""),
      "value_type": row.get("c8", ""),
  } for row in rows if row.get("section") == "syscall" and all(
      row.get(key) not in ("", None)
      for key in ("c0", "c1", "c2", "c3", "c4", "c5"))]
  return {
      "trace_health":
          summarize_trace_health(health_rows) if health_rows else None,
      "syscalls":
          build_syscall_events_from_rows(syscall_rows, analyzer),
  }


def build_mmap_validation_lifecycle(syscalls, pid: int):
  """把 syscall enter/exit 配对成无栈 mmap 生命周期事件。"""
  import mmap_phys_analyzer as analyzer

  pending = {}
  events = []
  for ev in syscalls:
    kind = analyzer.syscall_kind(ev)
    if kind is None:
      continue
    if analyzer.is_enter(ev) and not analyzer.is_exit(ev):
      pending.setdefault(ev.utid, []).append((kind, ev))
      continue
    if not analyzer.is_exit(ev):
      continue

    stack = pending.get(ev.utid, [])
    if not stack:
      continue
    kind, enter = stack.pop()
    if ev.ret is None or ev.ret < 0:
      continue

    if kind == "mmap":
      size = analyzer.first_int(enter.args, ("arg1", "len", "length"))
      if size is None:
        size = 0
      if size < 0:
        continue
      events.append((ev.ts, "mmap", {
          "pid": pid,
          "addr": ev.ret,
          "size": size,
          "stack_id": 0,
          "path": "",
      }))
    elif kind == "munmap":
      addr = analyzer.first_int(enter.args, ("arg0", "addr", "start"))
      size = analyzer.first_int(enter.args, ("arg1", "len", "length"))
      if addr is None or size is None or size <= 0:
        continue
      events.append((ev.ts, "munmap", {
          "pid": pid,
          "addr": addr,
          "size": size,
      }))
    elif kind == "mremap":
      old_addr = analyzer.first_int(enter.args, ("arg0", "old_address"))
      old_size = analyzer.first_int(enter.args, ("arg1", "old_size"))
      new_size = analyzer.first_int(enter.args, ("arg2", "new_size"))
      if old_addr is None or old_size is None or new_size is None:
        continue
      events.append((ev.ts, "mremap", {
          "pid": pid,
          "old_addr": old_addr,
          "old_size": old_size,
          "new_addr": ev.ret,
          "new_size": new_size,
          "stack_id": 0,
      }))
  return sorted(events, key=lambda event: event[0])


def build_mmap_summary_from_syscalls(syscalls, pid: int, smaps_dir: str):
  """用无栈 mmap 生命周期和最后一个 smaps 快照汇总 mmap PSS。"""
  import mmap_phys_analyzer as analyzer

  lifecycle = build_mmap_validation_lifecycle(syscalls, pid)
  snapshots = analyzer.load_snapshots(
      smaps_dir, pid=pid, unit="auto", offset_ns=0)
  if not snapshots:
    return {
        "pss_bytes": 0,
        "rss_bytes": 0,
        "virtual_bytes": 0,
        "syscall_events": len(syscalls),
        "lifecycle_events": len(lifecycle)
    }

  ranges = []
  event_index = 0
  final_stats = {}
  for snapshot in snapshots:
    while event_index < len(
        lifecycle) and lifecycle[event_index][0] <= snapshot.ts:
      ranges = analyzer.apply_event(ranges, lifecycle[event_index])
      event_index += 1
    final_stats = analyzer.attribute_snapshot(snapshot, ranges)

  summary_items = list(final_stats.values())
  return {
      "pss_bytes": int(sum(item.pss_bytes for item in summary_items)),
      "rss_bytes": int(sum(item.rss_bytes for item in summary_items)),
      "virtual_bytes": int(sum(item.virtual_bytes for item in summary_items)),
      "syscall_events": len(syscalls),
      "lifecycle_events": len(lifecycle),
      "smaps_snapshots": len(snapshots),
  }


def query_mmap_summary(trace_processor: str, trace_path: str, pid: int,
                       smaps_dir: str):
  """查询 mmap syscall 后汇总最终 smaps PSS。"""
  syscalls = query_mmap_validation_syscalls(trace_processor, trace_path, pid)
  return build_mmap_summary_from_syscalls(syscalls, pid, smaps_dir)


SMAPS_MEMINFO_CATEGORY_ORDER = (
    "Native Heap",
    "Dalvik Heap",
    "Stack",
    "Ashmem",
    "Other dev",
    ".so mmap",
    ".jar mmap",
    ".apk mmap",
    ".ttf mmap",
    ".dex mmap",
    ".oat mmap",
    ".art mmap",
    "Other mmap",
    "Unknown",
)

MEMINFO_MAIN_TABLE_EXTRA_ROWS = (
    "Dalvik Other",
    "Gfx dev",
    "EGL mtrack",
    "GL mtrack",
    "Other mtrack",
)

MEMINFO_MAIN_TABLE_ROW_NAMES = (
    set(SMAPS_MEMINFO_CATEGORY_ORDER) | set(MEMINFO_MAIN_TABLE_EXTRA_ROWS) |
    {"TOTAL"}
)


def classify_smaps_path_for_meminfo(pathname: str) -> str:
  """把 smaps pathname 粗分到接近 dumpsys meminfo 主表的类别。"""
  path = (pathname or "").strip()
  if not path:
    return "Unknown"
  lower = path.lower()
  if (lower == "[heap]" or lower.startswith("[anon:scudo:") or
      lower.startswith("[anon:libc_malloc") or "jemalloc" in lower):
    return "Native Heap"
  if ".art" in lower:
    return ".art mmap"
  if ".oat" in lower:
    return ".oat mmap"
  if ".vdex" in lower or ".dex" in lower:
    return ".dex mmap"
  if ".so" in lower:
    return ".so mmap"
  if ".jar" in lower:
    return ".jar mmap"
  if ".apk" in lower:
    return ".apk mmap"
  if ".ttf" in lower or ".otf" in lower or ".ttc" in lower:
    return ".ttf mmap"
  if lower.startswith("[anon:dalvik") or lower.startswith("[anon:art"):
    return "Dalvik Heap"
  if lower.startswith("[stack") or lower.endswith(" stack]"):
    return "Stack"
  if "ashmem" in lower:
    return "Ashmem"
  if lower.startswith("/dev/") or "dmabuf" in lower:
    return "Other dev"
  if lower.startswith("[anon:") or lower.startswith("[anon_shmem:"):
    return "Unknown"
  return "Other mmap"


def find_latest_smaps_path(smaps_dir: str) -> Optional[str]:
  """返回时间戳最大的 smaps 文件。"""
  if not smaps_dir or not os.path.isdir(smaps_dir):
    return None
  import mmap_phys_analyzer as analyzer

  candidates = []
  for root, _, files in os.walk(smaps_dir):
    for file_name in files:
      path = os.path.join(root, file_name)
      try:
        ts = analyzer.parse_timestamp_from_name(path, "auto")
      except ValueError:
        continue
      candidates.append((ts, path))
  if not candidates:
    return None
  return max(candidates, key=lambda item: item[0])[1]


def summarize_smaps_snapshot(smaps_path: str):
  """按 meminfo 近似类别汇总一份 smaps 快照。"""
  import mmap_phys_analyzer as analyzer

  categories = {}
  for vma in analyzer.parse_smaps(smaps_path):
    name = classify_smaps_path_for_meminfo(vma.pathname)
    item = categories.setdefault(name, {
        "name": name,
        "pss_bytes": 0,
        "rss_bytes": 0,
        "virtual_bytes": 0,
        "private_dirty_bytes": 0,
        "private_clean_bytes": 0,
        "shared_dirty_bytes": 0,
        "shared_clean_bytes": 0,
        "vma_count": 0,
    })
    item["pss_bytes"] += vma.pss_kb * 1024
    item["rss_bytes"] += vma.rss_kb * 1024
    item["virtual_bytes"] += max(0, vma.end - vma.start)
    item["private_dirty_bytes"] += vma.private_dirty_kb * 1024
    item["private_clean_bytes"] += vma.private_clean_kb * 1024
    item["shared_dirty_bytes"] += vma.shared_dirty_kb * 1024
    item["shared_clean_bytes"] += vma.shared_clean_kb * 1024
    item["vma_count"] += 1

  ordered = sorted(
      categories.values(),
      key=lambda item: (-item["pss_bytes"], item["name"]))
  return {
      "path":
          smaps_path,
      "total_pss_bytes":
          sum(item["pss_bytes"] for item in ordered),
      "total_rss_bytes":
          sum(item["rss_bytes"] for item in ordered),
      "total_virtual_bytes":
          sum(item["virtual_bytes"] for item in ordered),
      "categories":
          ordered,
  }


def build_smaps_meminfo_categories(smaps_summary, meminfo_rows):
  smaps_by_name = {
      item["name"]: item for item in smaps_summary.get("categories", [])
  } if smaps_summary else {}
  names = []
  for name in SMAPS_MEMINFO_CATEGORY_ORDER:
    if name in smaps_by_name or name in meminfo_rows:
      names.append(name)
  for name in sorted(set(smaps_by_name) | set(meminfo_rows)):
    if name not in names and name != "TOTAL":
      names.append(name)

  categories = []
  for name in names:
    smaps_pss = int(smaps_by_name.get(name, {}).get("pss_bytes", 0))
    meminfo_pss = int(meminfo_rows.get(name, {}).get("pss_bytes", 0))
    categories.append({
        "name": name,
        "smaps_pss_bytes": smaps_pss,
        "meminfo_pss_bytes": meminfo_pss,
        "smaps_minus_meminfo_pss_bytes": smaps_pss - meminfo_pss,
        "smaps_vma_count": int(smaps_by_name.get(name, {}).get("vma_count", 0)),
    })
  return categories


def build_memory_health_report(mmap_summary,
                               meminfo_summary,
                               meminfo_rows,
                               trace_health=None,
                               smaps_dir: Optional[str] = None):
  validation = build_memory_validation_status(mmap_summary, trace_health)
  smaps_path = find_latest_smaps_path(smaps_dir) if smaps_dir else None
  smaps_summary = summarize_smaps_snapshot(smaps_path) if smaps_path else None
  categories = build_smaps_meminfo_categories(smaps_summary, meminfo_rows)
  smaps_by_name = {
      item["name"]: item for item in smaps_summary.get("categories", [])
  } if smaps_summary else {}

  meminfo_total = int(meminfo_summary.get("total_pss_bytes", 0))
  smaps_total = int(smaps_summary.get("total_pss_bytes", 0)) if smaps_summary else 0
  native_heap_smaps = int(smaps_by_name.get("Native Heap", {}).get(
      "pss_bytes", 0))
  native_heap_meminfo = int(meminfo_summary.get("native_heap_pss_bytes", 0))
  health_checks = {
      "perfetto_trace_buffer": {
          "status":
              "pass" if int_value((trace_health or {}).get(
                  "perfetto_data_loss")) == 0 else "fail",
          "data_loss":
              int_value((trace_health or {}).get("perfetto_data_loss")),
      },
      "ftrace_kernel_buffer": {
          "status":
              "pass" if int_value((trace_health or {}).get(
                  "ftrace_data_loss")) == 0 else "fail",
          "data_loss":
              int_value((trace_health or {}).get("ftrace_data_loss")),
          "read_events":
              int_value((trace_health or {}).get("ftrace_read_events")),
      },
      "perf_callstack_buffer": {
          "status":
              "pass" if int_value((trace_health or {}).get(
                  "perf_data_loss")) == 0 else "fail",
          "data_loss":
              int_value((trace_health or {}).get("perf_data_loss")),
      },
      "traced_perf_profiler": {
          "status":
              "pass" if int_value((trace_health or {}).get(
                  "perf_samples_skipped_dataloss")) == 0 else "fail",
          "data_loss":
              int_value((trace_health or {}).get(
                  "perf_samples_skipped_dataloss")),
      },
      "perf_callstacks": {
          "status":
              ("not_checked" if "perf_callsites" not in (trace_health or {})
               else "pass" if int_value((trace_health or {}).get(
                   "perf_callsites")) > 0 else "fail"),
          "samples":
              int_value((trace_health or {}).get("perf_samples")),
          "callsites":
              int_value((trace_health or {}).get("perf_callsites")),
      },
      "mmap_syscalls": {
          "status":
              "pass" if int_value(mmap_summary.get("syscall_events")) > 0 else
              "fail",
          "events":
              int_value(mmap_summary.get("syscall_events")),
          "lifecycle_events":
              int_value(mmap_summary.get("lifecycle_events")),
      },
      "smaps": {
          "status":
              "pass" if int_value(mmap_summary.get("smaps_snapshots")) > 0 else
              "fail",
          "snapshots":
              int_value(mmap_summary.get("smaps_snapshots")),
          "latest_path":
              smaps_path or "",
      },
  }
  return {
      "units":
          "bytes",
      "health": {
          "status":
              validation["status"],
          "issues":
              validation["issues"],
          "checks":
              health_checks,
          "explanation": [
              ("健康状态只说明 mmap syscall events、smaps 快照和 Perfetto "
               "buffer 在本次采集中是否可用。"),
              ("mmap PSS 是 mmap 生命周期与 smaps VMA 地址重叠后的总量，"
               "不是 Android meminfo Native Heap 的同义词。"),
              ("Native Heap 对齐主要看 smaps 中 Native Heap 类别 "
               "([anon:scudo:*] 等) 与 dumpsys meminfo Native Heap PSS。"),
          ],
      },
      "alignment": {
          "mmap_pss_bytes": int_value(mmap_summary.get("pss_bytes")),
          "smaps": {
              "latest_path": smaps_path or "",
              "total_pss_bytes": smaps_total,
              "total_rss_bytes": int(
                  smaps_summary.get("total_rss_bytes", 0)
              ) if smaps_summary else 0,
          },
          "meminfo": {
              "total_pss_bytes": meminfo_total,
              "native_heap_pss_bytes": native_heap_meminfo,
              "native_heap_alloc_bytes": int(
                  meminfo_summary.get("native_heap_alloc_bytes", 0)),
          },
          "native_heap": {
              "smaps_pss_bytes": native_heap_smaps,
              "meminfo_pss_bytes": native_heap_meminfo,
              "smaps_minus_meminfo_pss_bytes":
                  native_heap_smaps - native_heap_meminfo,
          },
          "total": {
              "smaps_pss_bytes": smaps_total,
              "meminfo_total_pss_bytes": meminfo_total,
              "smaps_minus_meminfo_total_pss_bytes":
                  smaps_total - meminfo_total,
              "note":
                  ("smaps 总 PSS 不包含 memtrack HAL 上报的 GL/EGL mtrack 等"
                   "非 VMA 口径，通常不要求等于 meminfo TOTAL PSS。"),
          },
          "categories":
              categories,
      },
  }


def markdown_cell(value) -> str:
  return str(value).replace("\n", " ").replace("|", "\\|")


def markdown_table(headers, rows) -> list[str]:
  lines = [
      "| " + " | ".join(markdown_cell(header) for header in headers) + " |",
      "| " + " | ".join("---" for _ in headers) + " |",
  ]
  for row in rows:
    lines.append("| " + " | ".join(markdown_cell(cell) for cell in row) + " |")
  return lines


def build_memory_health_report_markdown(report) -> str:
  health = report["health"]
  checks = health["checks"]
  alignment = report["alignment"]
  native = alignment["native_heap"]
  ranked = sorted(
      alignment["categories"],
      key=lambda item: abs(item["smaps_pss_bytes"]) + abs(
          item["meminfo_pss_bytes"]),
      reverse=True)
  lines = [
      "# mmap 健康报告",
      "",
      "## 1. 健康说明",
      "",
  ]
  lines.extend(
      markdown_table(("项目", "值"), (("status", health["status"]),
                                  ("issues", ", ".join(health["issues"]) or
                                   "none"))))
  lines.extend(["", "### 采集检查", ""])
  lines.extend(
      markdown_table((
          "检查项",
          "状态",
          "数据",
      ), (
          ("Perfetto trace buffer", checks["perfetto_trace_buffer"]["status"],
           f"data_loss={checks['perfetto_trace_buffer']['data_loss']}"),
          ("ftrace kernel buffer", checks["ftrace_kernel_buffer"]["status"],
           "data_loss={data_loss}, read_events={read_events}".format(
               **checks["ftrace_kernel_buffer"])),
          ("linux.perf callstack buffer",
           checks["perf_callstack_buffer"]["status"],
           f"data_loss={checks['perf_callstack_buffer']['data_loss']}"),
          ("traced_perf profiler", checks["traced_perf_profiler"]["status"],
           f"data_loss={checks['traced_perf_profiler']['data_loss']}"),
          ("目标进程 perf 调用栈", checks["perf_callstacks"]["status"],
           "samples={samples}, callsites={callsites}".format(
               **checks["perf_callstacks"])),
          ("mmap syscalls", checks["mmap_syscalls"]["status"],
           "events={events}, lifecycle={lifecycle_events}".format(
               **checks["mmap_syscalls"])),
          ("smaps", checks["smaps"]["status"],
           "snapshots={snapshots}, latest={latest_path}".format(
               **checks["smaps"])),
      )))
  lines.extend(["", "### 说明", ""])
  for item in health.get("explanation", []):
    lines.append(f"- {item}")

  lines.extend(["", "## 2. smaps 与 meminfo 对齐", ""])
  lines.extend(
      markdown_table(("指标", "值"), (
          ("latest smaps", alignment["smaps"]["latest_path"] or "-"),
          ("mmap PSS", format_mib(alignment["mmap_pss_bytes"])),
          ("smaps total PSS", format_mib(alignment["smaps"]["total_pss_bytes"])),
          ("smaps total RSS", format_mib(alignment["smaps"]["total_rss_bytes"])),
          ("meminfo TOTAL PSS",
           format_mib(alignment["meminfo"]["total_pss_bytes"])),
          ("Native Heap smaps PSS", format_mib(native["smaps_pss_bytes"])),
          ("Native Heap meminfo PSS", format_mib(native["meminfo_pss_bytes"])),
          ("Native Heap delta",
           format_mib(native["smaps_minus_meminfo_pss_bytes"])),
          ("smaps - meminfo TOTAL",
           format_mib(alignment["total"]
                      ["smaps_minus_meminfo_total_pss_bytes"])),
      )))
  lines.extend(["", "### smaps 分类", ""])
  lines.extend(
      markdown_table((
          "类别",
          "smaps PSS",
          "meminfo PSS",
          "delta",
          "VMA 数",
      ), [(
          item["name"],
          format_mib(item["smaps_pss_bytes"]),
          format_mib(item["meminfo_pss_bytes"]),
          format_mib(item["smaps_minus_meminfo_pss_bytes"]),
          item["smaps_vma_count"],
      ) for item in ranked]))
  lines.extend(["", f"> {alignment['total']['note']}"])
  return "\n".join(lines) + "\n"


def print_memory_health_report(report):
  print(build_memory_health_report_markdown(report), end="")


def build_memory_validation_status(mmap_summary, trace_health):
  issues = []
  if int_value(mmap_summary.get("smaps_snapshots")) > 0 and int_value(
      mmap_summary.get("syscall_events")) == 0:
    issues.append("mmap_syscall_events_missing")
  if trace_health:
    if int_value(trace_health.get("perfetto_data_loss")) > 0:
      issues.append("perfetto_data_loss")
    if int_value(trace_health.get("ftrace_data_loss")) > 0:
      issues.append("ftrace_data_loss")
    if int_value(trace_health.get("perf_data_loss")) > 0:
      issues.append("perf_data_loss")
    if int_value(trace_health.get("perf_samples_skipped_dataloss")) > 0:
      issues.append("perf_samples_skipped_dataloss")
    perf_callsites = int_value(trace_health.get("perf_callsites"))
    if "perf_callsites" in trace_health and perf_callsites == 0:
      issues.append("perf_callstacks_missing")
  return {
      "status": "fail" if issues else "pass",
      "issues": issues,
  }


def write_memory_validation_report(output_dir: str,
                                   mmap_summary,
                                   meminfo_path: str,
                                   trace_health=None,
                                   smaps_dir: Optional[str] = None) -> str:
  with open(meminfo_path, "r", encoding="utf-8") as fd:
    meminfo_text = fd.read()
  meminfo = parse_meminfo_summary(meminfo_text)
  meminfo_rows = parse_meminfo_table_rows(meminfo_text)
  health_report = build_memory_health_report(
      mmap_summary,
      meminfo,
      meminfo_rows,
      trace_health=trace_health,
      smaps_dir=smaps_dir)
  report = {
      "units": "bytes",
      "note": ("验证只检查 mmap syscall events + smaps 的采集健康；"
               "不采集 malloc profile，也不做 Native Heap Alloc 对比。"),
      "mmap": mmap_summary,
      "trace_health": trace_health or {},
      "validation": build_memory_validation_status(mmap_summary, trace_health),
      "meminfo": meminfo,
      "comparison": {},
      "health_report": health_report,
      "sources": {
          "meminfo": meminfo_path,
          "mmap": "mmap syscall events + smaps",
      },
  }
  health_path = os.path.join(output_dir, "mmap_health_report.json")
  with open(health_path, "w", encoding="utf-8") as fd:
    json.dump(health_report, fd, ensure_ascii=False, indent=2)
    fd.write("\n")
  health_md_path = os.path.join(output_dir, "mmap_health_report.md")
  with open(health_md_path, "w", encoding="utf-8") as fd:
    fd.write(build_memory_health_report_markdown(health_report))
  path = os.path.join(output_dir, "memory_validation.json")
  with open(path, "w", encoding="utf-8") as fd:
    json.dump(report, fd, ensure_ascii=False, indent=2)
    fd.write("\n")
  print("内存验证:")
  print(f"  mmap PSS: {format_mib(report['mmap'].get('pss_bytes', 0))}")
  print(f"  meminfo Native Heap PSS: "
        f"{format_mib(meminfo['native_heap_pss_bytes'])}")
  print(f"  validation status: {report['validation']['status']}")
  if report["validation"]["issues"]:
    print("  validation issues: " + ", ".join(report["validation"]["issues"]))
  print(f"验证报告已保存: {path}")
  print_memory_health_report(health_report)
  print(f"健康报告已保存: {health_md_path}")
  print(f"健康报告 JSON 已保存: {health_path}")
  return path


def collect_memory_validation(args,
                              pid: int,
                              trace_path: str,
                              meminfo_path: str,
                              trace_health=None):
  mmap_summary = {"pss_bytes": 0, "rss_bytes": 0, "virtual_bytes": 0}
  combined_inputs = None

  if args.trace_processor:
    try:
      combined_inputs = query_memory_validation_inputs(args.trace_processor,
                                                       trace_path, pid)
    except RuntimeError as exc:
      print(str(exc), file=sys.stderr)

  if combined_inputs is not None:
    if trace_health is None:
      trace_health = combined_inputs.get("trace_health")
      if trace_health:
        print_trace_health(trace_health)
    mmap_summary = build_mmap_summary_from_syscalls(
        combined_inputs["syscalls"], pid, os.path.join(args.output, "smaps"))
  else:
    print("跳过 mmap 汇总：未指定 --trace-processor", file=sys.stderr)

  if not meminfo_path:
    print("跳过 meminfo 对比：采样结束后未成功保存 dumpsys meminfo", file=sys.stderr)
    return {
        "status": 1,
        "trace_health": trace_health,
        "report_path": "",
        "validation": {
            "status": "fail",
            "issues": ["meminfo_missing"],
        },
    }

  validation = build_memory_validation_status(mmap_summary, trace_health)
  report_path = write_memory_validation_report(
      args.output,
      mmap_summary,
      meminfo_path,
      trace_health,
      smaps_dir=os.path.join(args.output, "smaps"))
  return {
      "status": 0 if validation["status"] == "pass" else 1,
      "trace_health": trace_health,
      "report_path": report_path,
      "validation": validation,
  }


def run_analyzer(args, pid: int, trace_path: str, smaps_dir: str,
                 callstack_trace_path: Optional[str] = None):
  analyzer = args.analyzer
  if not analyzer:
    analyzer = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "mmap_phys_analyzer.py")
  if not os.path.exists(analyzer):
    print(f"跳过分析：找不到 {analyzer}", file=sys.stderr)
    return

  cmd = [
      sys.executable,
      analyzer,
      "--trace",
      trace_path,
      "--smaps-dir",
      smaps_dir,
      "--pid",
      str(pid),
      "--output",
      os.path.join(args.output, "mmap_phys_attribution.json"),
      "--pprof-output",
      os.path.join(args.output, "mmap_phys_attribution.pprof.pb.gz"),
  ]
  if callstack_trace_path:
    cmd.extend(["--callstack-trace", callstack_trace_path])
  if args.trace_processor:
    cmd.extend(["--trace-processor", args.trace_processor])
  if getattr(args, "classify_config", None):
    cmd.extend(["--classify-config", args.classify_config])
    cmd.extend(
        ["--classify-summary-pprof-out",
         "mmap_classification_summary.pprof.pb.gz"])
    cmd.extend(["--classify-pprof-dir", "pprof_categories"])
  top_n = getattr(args, "top_n", None)
  if top_n is not None:
    cmd.extend(["--top-n", str(top_n)])
  subprocess.check_call(cmd)


def parse_args():
  parser = argparse.ArgumentParser(
      description="采集 mmap/perf/smaps 数据，并生成 mmap 物理内存归因结果")
  parser.add_argument("-n", "--name", required=True, help="目标进程名/包名")
  parser.add_argument(
      "-d",
      "--duration-ms",
      type=int,
      default=75000,
      help="Perfetto 采集时长，单位 ms")
  parser.add_argument(
      "--smaps-interval-ms", type=int, default=1000, help="smaps 采样间隔，单位 ms")
  parser.add_argument("-o", "--output", default=None, help="输出目录")
  parser.add_argument(
      "--wait-timeout-s", type=int, default=120, help="等待目标进程启动的超时时间；0 表示无限等待")
  parser.add_argument(
      "--buffer-kb",
      type=int,
      default=262144,
      help="Perfetto ring buffer 大小，单位 KiB")
  parser.add_argument(
      "--perf-ring-buffer-pages",
      type=int,
      default=32768,
      help="linux.perf 每 CPU ring buffer 页数；0 表示使用 Perfetto 默认值")
  parser.add_argument(
      "--perf-ring-buffer-read-period-ms",
      type=int,
      default=25,
      help="linux.perf ring buffer 读取周期；0 表示使用 Perfetto 默认值")
  parser.add_argument(
      "--mmap-callstacks",
      dest="mmap_callstacks",
      action="store_true",
      default=True,
      help="额外采集 mmap 调用栈并运行 mmap 物理归因火焰图分析")
  parser.add_argument(
      "--no-mmap-callstacks",
      dest="mmap_callstacks",
      action="store_false",
      help="不采集 mmap 调用栈；仅运行无栈 mmap 事件健康检查")
  parser.add_argument(
      "--no-ftrace",
      action="store_true",
      help="不启用 linux.ftrace syscall_events；会跳过无栈 mmap 验证")
  parser.add_argument(
      "--no-kernel-frames", action="store_true", help="mmap 调用栈不采内核帧")
  parser.add_argument(
      "--no-guardrails",
      action="store_true",
      help="传递给 perfetto 的 --no-guardrails")
  parser.add_argument(
      "--use-su", action="store_true", help="通过 su 0 读取 /proc/<pid>/smaps")
  parser.add_argument(
      "--no-analyze", action="store_true", help="只采集 trace 和 smaps，不运行离线分析器")
  parser.add_argument(
      "--trace-processor", help="传给 mmap_phys_analyzer.py 的 trace_processor 路径")
  parser.add_argument(
      "--traceconv", help="traceconv 路径；主功能用于生成 symbolized-trace")
  parser.add_argument(
      "--classify-config", help="传给 mmap_phys_analyzer.py 的 fs.ini 分类配置")
  parser.add_argument(
      "--top-n",
      type=int,
      default=None,
      help="传给 mmap_phys_analyzer.py 的调用栈输出数量；0 表示全部")
  parser.add_argument("--analyzer", help="mmap_phys_analyzer.py 路径")
  return parser.parse_args()


def prepare_output_dir(output_dir: str) -> bool:
  os.makedirs(output_dir, exist_ok=True)
  if os.listdir(output_dir):
    print(f"FATAL: 输出目录非空: {output_dir}", file=sys.stderr)
    return False
  return True


def finish_collection(args, pid: int, device_trace: str, trace_path: str,
                      smaps_dir: str,
                      callstack_device_trace: Optional[str] = None):
  """拉回 trace，并复用同一套 meminfo、符号化、分析和健康验证。"""
  if callstack_device_trace:
    callstack_trace_path = os.path.join(
        args.output, "mmap_callstack_trace.perfetto-trace")
    pull_trace(device_trace, trace_path)
    pull_trace(callstack_device_trace, callstack_trace_path)
  else:
    pull_trace(device_trace, trace_path)
  try:
    meminfo_path = capture_meminfo(args.name, args.output)
  except subprocess.CalledProcessError as exc:
    meminfo_path = ""
    print("跳过 meminfo 对比，dumpsys meminfo 失败:", file=sys.stderr)
    print(exc.output.decode("utf-8", errors="replace"), file=sys.stderr)

  analysis_callstack_trace_path = trace_path
  if args.mmap_callstacks:
    analysis_callstack_trace_path = symbolize_trace(
        getattr(args, "traceconv", None),
        callstack_trace_path if callstack_device_trace else trace_path,
        args.output)

  trace_health = None
  if args.mmap_callstacks:
    trace_health = check_trace_health(args, trace_path)
    callstack_health = (
        check_trace_health(args, callstack_trace_path, pid)
        if callstack_device_trace else check_trace_health(args, trace_path, pid))
    if trace_health is None:
      trace_health = callstack_health
    elif callstack_health and callstack_device_trace:
      trace_health["buffer_size_bytes"] = int(
          trace_health.get("buffer_size_bytes", 0)) + int(
          callstack_health.get("buffer_size_bytes", 0))
      trace_health["bytes_written"] = int(
          trace_health.get("bytes_written", 0)) + int(
          callstack_health.get("bytes_written", 0))
      trace_health["perfetto_data_loss"] = int(
          trace_health.get("perfetto_data_loss", 0)) + int(
          callstack_health.get("perfetto_data_loss", 0))
      for key in (
          "perf_data_loss", "perf_samples_skipped_dataloss",
          "perf_samples", "perf_callsites"):
        if key in callstack_health:
          trace_health[key] = callstack_health[key]
      print("合并后的 mmap 主功能健康检查:")
      print_trace_health(trace_health)
    elif callstack_health:
      trace_health.update(callstack_health)

  if args.mmap_callstacks and not args.no_analyze:
    if callstack_device_trace:
      run_analyzer(
          args,
          pid,
          trace_path,
          smaps_dir,
          callstack_trace_path=analysis_callstack_trace_path)
    else:
      run_analyzer(args, pid, analysis_callstack_trace_path, smaps_dir)
  elif not args.mmap_callstacks and not args.no_analyze:
    print("跳过 mmap 调用栈分析：当前验证模式未采集 mmap 调用栈")

  validation = collect_memory_validation(args, pid, trace_path, meminfo_path,
                                         trace_health)
  print("采集完成")
  print(f"输出目录: {os.path.abspath(args.output)}")
  validation = validation or {"trace_health": trace_health, "report_path": ""}
  callstacks_missing = (
      args.mmap_callstacks and trace_health is not None and
      "perf_callsites" in trace_health and
      int_value(trace_health["perf_callsites"]) == 0)
  validation["status"] = max(
      int_value(validation.get("status")), 1 if callstacks_missing else 0)
  if callstacks_missing:
    print(
        "MMAP_PROFILE_FAILED|reason=perf_callstacks_missing|"
        f"pid={pid}|samples={int_value((trace_health or {}).get('perf_samples'))}|"
        "请检查 traced_perf 读取 /proc/<pid>/maps 的权限")
  elif validation["status"] != 0:
    issues = validation.get("validation", {}).get("issues", [])
    print(
        "MMAP_PROFILE_FAILED|reason=memory_validation_failed|issues="
        + ",".join(str(issue) for issue in issues))
  validation["output"] = args.output
  return validation


def run_collection(args, start_target_after_perfetto: bool = False):
  """执行一次 mmap 物理内存采集，并返回验证健康信息。"""
  if not prepare_output_dir(args.output):
    return {"status": 1, "trace_health": None, "report_path": ""}
  smaps_dir = os.path.join(args.output, "smaps")
  trace_path = os.path.join(args.output, "mmap_trace.perfetto-trace")
  device_trace = f"/data/misc/perfetto-traces/mmap-phys-{int(time.time() * 1000)}"

  config = build_perfetto_config(
      name=args.name,
      duration_ms=args.duration_ms,
      buffer_kb=args.buffer_kb,
      include_ftrace=not args.no_ftrace,
      kernel_frames=not args.no_kernel_frames,
      perf_ring_buffer_pages=args.perf_ring_buffer_pages,
      perf_ring_buffer_read_period_ms=args.perf_ring_buffer_read_period_ms,
      include_mmap_callstacks=args.mmap_callstacks)
  write_config(config, args.output)
  if start_target_after_perfetto:
    # 验证 attempt 必须先让 ftrace 就绪，再启动 App，
    # 否则启动期 mmap 会被漏采，事件健康检查没有意义。
    perfetto_pid = start_perfetto(config, device_trace, args.no_guardrails)
    pid = wait_for_pid(args.name, args.wait_timeout_s)
  else:
    pid = wait_for_pid(args.name, args.wait_timeout_s)
    perfetto_pid = start_perfetto(config, device_trace, args.no_guardrails)

  try:
    collect_smaps(
        pid,
        perfetto_pid,
        smaps_dir,
        args.smaps_interval_ms,
        args.use_su,
        args.duration_ms,
        run_as_package=args.name)
  finally:
    if IS_INTERRUPTED:
      stop_perfetto(perfetto_pid)

  return finish_collection(args, pid, device_trace, trace_path, smaps_dir)


def run_profile_controlled_collection(args, action_module):
  """主调用栈模式：由登录后的测试协程决定采集结束时间。"""
  if not prepare_output_dir(args.output):
    return {"status": 1, "trace_health": None, "report_path": ""}
  smaps_dir = os.path.join(args.output, "smaps")
  trace_path = os.path.join(args.output, "mmap_trace.perfetto-trace")
  trace_id = int(time.time() * 1000)
  device_trace = f"/data/misc/perfetto-traces/mmap-lifecycle-{trace_id}"
  callstack_device_trace = (
      f"/data/misc/perfetto-traces/mmap-callstack-{trace_id}")
  lifecycle_config = build_perfetto_config(
      name=args.name,
      duration_ms=0,
      buffer_kb=args.buffer_kb,
      include_ftrace=not args.no_ftrace,
      kernel_frames=not args.no_kernel_frames,
      perf_ring_buffer_pages=args.perf_ring_buffer_pages,
      perf_ring_buffer_read_period_ms=args.perf_ring_buffer_read_period_ms,
      include_mmap_callstacks=False)
  callstack_config = build_perfetto_config(
      name=args.name,
      duration_ms=0,
      buffer_kb=args.buffer_kb,
      include_ftrace=False,
      kernel_frames=not args.no_kernel_frames,
      perf_ring_buffer_pages=args.perf_ring_buffer_pages,
      perf_ring_buffer_read_period_ms=args.perf_ring_buffer_read_period_ms,
      include_mmap_callstacks=True)
  write_config(lifecycle_config, args.output, "mmap_lifecycle_config.pbtxt")
  write_config(callstack_config, args.output, "mmap_callstack_config.pbtxt")

  lifecycle_perfetto_pid = None
  callstack_perfetto_pid = None
  root_traced_perf_state = None
  logcat_process = logcat_stdout = logcat_stderr = None
  smaps_thread = None
  smaps_stop = threading.Event()
  smaps_errors = []
  stage_failed = False
  pid = 0
  try:
    # 生命周期会话先于 App，完整记录启动期 mmap；调用栈会话等 PID 出现后再启，
    # 避免首个 perf sample 在 /proc 描述符尚未可用时永久进入 FdsTimedOut。
    root_traced_perf_state = start_root_traced_perf()
    lifecycle_perfetto_pid = start_perfetto(
        lifecycle_config, device_trace, args.no_guardrails)
    force_stop_app(args.name)
    logcat_process, logcat_stdout, logcat_stderr = start_logcat_capture(
        args.output)
    pid = wait_for_pid(args.name, args.wait_timeout_s)
    callstack_perfetto_pid = start_perfetto(
        callstack_config, callstack_device_trace, args.no_guardrails)
    def collect_smaps_in_background():
      try:
        collect_smaps(
            pid, lifecycle_perfetto_pid, smaps_dir, args.smaps_interval_ms,
            args.use_su, 0, run_as_package=args.name,
            stop_event=smaps_stop)
      except Exception as exc:  # noqa: BLE001 - 主线程统一收尾后返回失败。
        smaps_errors.append(exc)

    smaps_thread = threading.Thread(
        target=collect_smaps_in_background, daemon=True)
    smaps_thread.start()

    logcat_path = os.path.join(args.output, "logcat.txt")
    context = ProfileActionContext(
        app=args.name,
        pid=pid,
        output_dir=Path(args.output).resolve(),
        logcat_path=Path(logcat_path).resolve(),
        adb=os.environ.get("ADB_BINARY", "adb"),
        rpc_local_port=RPC_LOCAL_PORT,
        android_serial=os.environ.get("ANDROID_SERIAL", ""),
        rpc_timeout_seconds=float(
            os.environ.get("HEAP_PROFILE_RPC_TIMEOUT_S", "30")),
        summary_path=Path(args.output, "run_summary.txt").resolve(),
    )
    result = run_profile_action_module(
        action_module,
        context,
        lambda: IS_INTERRUPTED,
        lambda: is_process_alive(pid),
    )
    stage_failed = not result.success
  except Exception as exc:  # noqa: BLE001 - 必须先收尾 trace 再返回失败。
    print(
        f"MMAP_PROFILE_FAILED|reason=collection_control_failed|"
        f"error={type(exc).__name__}: {exc}", file=sys.stderr)
    stage_failed = True
  finally:
    smaps_stop.set()
    if smaps_thread:
      smaps_thread.join(timeout=max(5.0, args.smaps_interval_ms / 1000.0 + 2))
      if smaps_thread.is_alive():
        smaps_errors.append(RuntimeError("smaps 线程未在超时内结束"))
    if smaps_errors:
      print(
          "MMAP_PROFILE_FAILED|reason=smaps_collection_failed|"
          f"error={type(smaps_errors[0]).__name__}: {smaps_errors[0]}",
          file=sys.stderr)
      stage_failed = True
    stop_logcat_capture(logcat_process, logcat_stdout, logcat_stderr)
    if callstack_perfetto_pid is not None:
      stop_perfetto(callstack_perfetto_pid)
    if lifecycle_perfetto_pid is not None:
      stop_perfetto(lifecycle_perfetto_pid)
    stop_root_traced_perf(root_traced_perf_state)

  try:
    validation = finish_collection(
        args, pid, device_trace, trace_path, smaps_dir,
        callstack_device_trace=(
            callstack_device_trace if callstack_perfetto_pid is not None else None))
  except Exception as exc:  # noqa: BLE001 - 保留阶段失败并输出离线收尾根因。
    print(
        "MMAP_PROFILE_FAILED|reason=finish_collection_failed|"
        f"error={type(exc).__name__}: {exc}", file=sys.stderr)
    return {"status": 1, "trace_health": None, "report_path": ""}
  if stage_failed:
    validation["status"] = 1
  return validation


def main() -> int:
  signal.signal(signal.SIGINT, on_signal)
  signal.signal(signal.SIGTERM, on_signal)
  args = parse_args()

  if args.output is None:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    args.output = os.path.join("PerfData", "mmap_phys", stamp)

  if not args.mmap_callstacks:
    force_stop_app(args.name)
    return int(
        run_collection(args, start_target_after_perfetto=True).get("status", 1))

  script_dir = Path(__file__).resolve().parent
  try:
    action_path = resolve_action_module_path(
        script_dir, os.environ.get("PERF_PROFILE_ACTION_SCRIPT", ""))
    action_module = load_action_module(action_path)
  except (OSError, RuntimeError) as exc:
    print(f"MMAP_PROFILE_FAILED|reason=action_script_invalid|error={exc}")
    return 1
  print(
      "MMAP_PROFILE_CONFIG|"
      f"app={args.name}|buffer_kb={args.buffer_kb}|"
      f"smaps_interval_ms={args.smaps_interval_ms}|action_script={action_path}")
  return int(run_profile_controlled_collection(args, action_module).get(
      "status", 1))


if __name__ == "__main__":
  sys.exit(main())
