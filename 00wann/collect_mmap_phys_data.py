#!/usr/bin/env python3
"""采集 mmap 物理内存归因所需的数据。

脚本会同时完成三件事：
1. 启动 Perfetto，采集 mmap/munmap/mremap syscall 和 mmap 调用栈。
2. 周期性拉取 /proc/<pid>/smaps，作为真实物理内存 PSS/RSS 快照。
3. 可选调用 mmap_phys_analyzer.py，生成 Perfetto JSON 和 Speedscope 火焰图。
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
import time
from datetime import datetime
from typing import Optional


ARM64_MMAP_NR = 222
ARM64_MUNMAP_NR = 215
ARM64_MREMAP_NR = 216

IS_INTERRUPTED = False
SMAPS_HEADER_RE = re.compile(rb"^[0-9a-fA-F]+-[0-9a-fA-F]+\s+")

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
    "ftrace_cpu_overrun_delta",
    "ftrace_cpu_commit_overrun_delta",
    "ftrace_cpu_dropped_events_delta",
    "ftrace_cpu_has_data_loss",
    "ftrace_cpu_read_events_delta",
    "heapprofd_buffer_overran",
    "heapprofd_client_error",
    "heapprofd_missing_packet",
    "heapprofd_non_finalized_profile",
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
  return subprocess.check_output(args, **kwargs).decode("utf-8", errors="replace")


def adb_shell(cmd: str, log: bool = True) -> str:
  return check_output(["adb", "shell", cmd], log=log, stderr=subprocess.STDOUT).strip()


def format_wait_status(name: str, elapsed_s: float) -> str:
  return f"等待应用启动: {name} 已等待 {elapsed_s:.1f}s"


def format_smaps_progress_line(path: str, size_bytes: int, remaining_s: float) -> str:
  return f"smaps 快照: {path} ({size_bytes} bytes) 剩余: {max(0.0, remaining_s):.1f}s"


def launch_app(name: str):
  """目标进程未运行时，通过 monkey 发送一次启动 Intent。"""
  print(f"目标进程未启动，尝试启动应用: {name}")
  adb_shell(f"monkey -p {shell_quote(name)} 1")


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
  print(f"WARN: force-stop 后目标进程仍存在，将继续尝试重新采集: {name}",
        file=sys.stderr)


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
                          perf_ring_buffer_pages: int = 4096,
                          perf_ring_buffer_read_period_ms: int = 100,
                          include_malloc: bool = False,
                          malloc_sampling_interval_bytes: int = 4096,
                          malloc_shmem_size_bytes: int = 8 * 1024 * 1024,
                          include_mmap_callstacks: bool = True) -> str:
  kernel_frames_value = "true" if kernel_frames else "false"
  perf_ring_buffer_block = ""
  if perf_ring_buffer_pages > 0:
    perf_ring_buffer_block += f"      ring_buffer_pages: {perf_ring_buffer_pages}\n"
  if perf_ring_buffer_read_period_ms > 0:
    perf_ring_buffer_block += (
        f"      ring_buffer_read_period_ms: {perf_ring_buffer_read_period_ms}\n")
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
  malloc_block = ""
  if include_malloc:
    malloc_block = f"""
data_sources {{
  config {{
    name: "android.heapprofd"
    heapprofd_config {{
      shmem_size_bytes: {malloc_shmem_size_bytes}
      sampling_interval_bytes: {malloc_sampling_interval_bytes}
      process_cmdline: "{name}"
      heaps: "libc.malloc"
    }}
  }}
}}
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
        kernel_frames: {kernel_frames_value}
      }}
{perf_ring_buffer_block.rstrip()}
    }}
  }}
}}
"""

  return f"""buffers {{
  size_kb: {buffer_kb}
  fill_policy: RING_BUFFER
}}

{ftrace_block}
{malloc_block}
{perf_block}

duration_ms: {duration_ms}
write_into_file: true
flush_period_ms: 5000
flush_timeout_ms: 30000
"""


def write_config(config: str, output_dir: str) -> str:
  host_path = os.path.join(output_dir, "mmap_phys_config.pbtxt")
  with open(host_path, "w", encoding="utf-8") as fd:
    fd.write(config)
  print(f"Perfetto 配置已保存: {host_path}")
  return host_path


def start_perfetto(config: str, device_trace: str, no_guardrails: bool) -> int:
  cmd = [
      "adb", "shell", "perfetto", "--txt", "-c", "-",
      "-o", device_trace, "-d"
  ]
  if no_guardrails:
    cmd.append("--no-guardrails")
  print("+ " + " ".join(cmd) + " < mmap_phys_config.pbtxt")
  try:
    out = check_output(
        cmd,
        log=False,
        input=config.encode("utf-8"),
        stderr=subprocess.STDOUT)
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


def device_time_ns(log: bool = True) -> int:
  uptime = adb_shell("cat /proc/uptime", log=log).split()[0]
  return int(float(uptime) * 1_000_000_000)


def is_process_alive(pid: int) -> bool:
  return subprocess.call(
      ["adb", "shell", f"[ -d /proc/{pid} ]"],
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


def read_smaps(pid: int, use_su: bool, run_as_package: Optional[str] = None) -> bytes:
  data = read_smaps_with_mode(pid, use_su)
  if is_valid_smaps(data):
    return data
  if not use_su:
    if run_as_package:
      print(f"普通权限读取 smaps 无效，自动尝试 run-as {run_as_package}",
            file=sys.stderr)
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
                  run_as_package: Optional[str] = None):
  os.makedirs(smaps_dir, exist_ok=True)
  interval_s = interval_ms / 1000.0
  duration_s = duration_ms / 1000.0
  start_s = time.monotonic()
  sample_count = 0
  progress = TwoLineProgress()
  while not IS_INTERRUPTED and is_process_alive(perfetto_pid):
    ts_ns = device_time_ns(log=False)
    path = os.path.join(smaps_dir, f"{ts_ns}.smaps")
    try:
      data = read_smaps(pid, use_su, run_as_package=run_as_package)
      with open(path, "wb") as fd:
        fd.write(data)
      sample_count += 1
      remaining_s = duration_s - (time.monotonic() - start_s)
      progress.update("+ adb shell cat /proc/uptime",
                      format_smaps_progress_line(path, len(data), remaining_s))
    except subprocess.CalledProcessError as exc:
      print(f"读取 smaps 失败: {exc.output.decode('utf-8', errors='replace')}",
            file=sys.stderr)
    except RuntimeError as exc:
      print(f"读取 smaps 失败: {exc}", file=sys.stderr)
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


def pull_trace(device_trace: str, host_trace: str):
  subprocess.check_call(["adb", "pull", device_trace, host_trace])
  subprocess.call(["adb", "shell", "rm", "-f", device_trace])
  print(f"trace 已保存: {host_trace}")


def parse_trace_processor_csv(output: str, required_columns=("name", "idx", "value")):
  """从 trace_processor 日志混合输出里截取 CSV 查询结果。"""
  # trace_processor 的耗时日志有时会直接贴在 CSV 最后一行后面，先按日志前缀截断。
  lines = [re.sub(r"\[\d+\.\d+\]\s+.*$", "", line)
           for line in output.splitlines()]
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
  sql = (
      "select name, idx, value from stats "
      f"where name in ({quoted_names}) "
      "order by name, idx")
  print("+ " + " ".join([trace_processor, "query", trace_path, sql]))
  proc = subprocess.run(
      [trace_processor, "query", trace_path, sql],
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      check=False)
  if proc.returncode != 0:
    print("Perfetto buffer 健康检查失败:", file=sys.stderr)
    print(proc.stdout, file=sys.stderr)
    return []
  return parse_trace_processor_csv(proc.stdout)


def summarize_trace_health(rows):
  def sum_stats(names):
    return sum(
        int(row["value"]) for row in rows
        if row.get("name") in names and row.get("value") not in ("", None))

  def max_stat(name):
    values = [
        int(row["value"]) for row in rows
        if row.get("name") == name and row.get("value") not in ("", None)
    ]
    return max(values) if values else 0

  # 分开统计三类 buffer，便于定位应该调哪个配置项。
  return {
      "buffer_size_bytes": max_stat("traced_buf_buffer_size"),
      "bytes_written": max_stat("traced_buf_bytes_written"),
      "perfetto_data_loss": sum_stats({
          "traced_buf_bytes_overwritten",
          "traced_buf_chunks_overwritten",
          "traced_buf_chunks_discarded",
          "traced_buf_trace_writer_packet_loss",
          "traced_buf_patches_failed",
          "traced_buf_abi_violations",
      }),
      "perf_data_loss": sum_stats({
          "perf_cpu_lost_records",
          "perf_aux_lost",
      }),
      "ftrace_data_loss": sum_stats({
          "ftrace_cpu_overrun_delta",
          "ftrace_cpu_commit_overrun_delta",
          "ftrace_cpu_dropped_events_delta",
          "ftrace_cpu_has_data_loss",
      }),
      "ftrace_read_events": sum_stats({
          "ftrace_cpu_read_events_delta",
      }),
      "heapprofd_data_loss": sum_stats({
          "heapprofd_buffer_overran",
          "heapprofd_missing_packet",
          "heapprofd_non_finalized_profile",
      }),
      "heapprofd_errors": sum_stats({
          "heapprofd_client_error",
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
  if summary.get("heapprofd_data_loss", 0) == 0 and summary.get("heapprofd_errors", 0) == 0:
    print("  OK: heapprofd 未报告共享内存截断或客户端错误")
  else:
    print(f"  WARN: heapprofd 异常计数 data_loss={summary.get('heapprofd_data_loss', 0)}, "
          f"errors={summary.get('heapprofd_errors', 0)}，建议增大 "
          "--malloc-shmem-size-bytes 或提高 --malloc-sampling-interval-bytes")


def check_trace_health(args, trace_path: str):
  if not args.trace_processor:
    print("跳过 Perfetto buffer 健康检查：未指定 --trace-processor", file=sys.stderr)
    return
  rows = query_trace_health(args.trace_processor, trace_path)
  if not rows:
    print("跳过 Perfetto buffer 健康检查：stats 查询无结果", file=sys.stderr)
    return None
  summary = summarize_trace_health(rows)
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
        int(item.replace(",", ""))
        for item in re.findall(r"-?[\d,]+", line)
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


def capture_meminfo(name: str, output_dir: str) -> str:
  meminfo = adb_shell(f"dumpsys meminfo {shell_quote(name)}")
  path = os.path.join(output_dir, "dumpsys_meminfo.txt")
  with open(path, "w", encoding="utf-8") as fd:
    fd.write(meminfo)
    if not meminfo.endswith("\n"):
      fd.write("\n")
  print(f"dumpsys meminfo 已保存: {path}")
  return path


def query_malloc_summary(trace_processor: str, trace_path: str, pid: int):
  """只聚合 heap_profile_allocation，不查询 malloc 分配调用栈。"""
  sql = f"""
WITH target_process AS (
  SELECT upid
  FROM process
  WHERE pid = {pid}
  ORDER BY start_ts DESC
  LIMIT 1
)
SELECT
  h.heap_name AS heap_name,
  sum(h.size) AS live_bytes,
  sum(CASE WHEN h.size > 0 THEN h.size ELSE 0 END) AS allocated_bytes,
  sum(CASE WHEN h.size < 0 THEN -h.size ELSE 0 END) AS freed_bytes
FROM heap_profile_allocation h
JOIN target_process t ON h.upid = t.upid
WHERE h.heap_name IN ('libc.malloc', 'malloc')
GROUP BY h.heap_name
ORDER BY h.heap_name
"""
  print("+ " + " ".join([trace_processor, "query", trace_path, sql]))
  proc = subprocess.run(
      [trace_processor, "query", trace_path, sql],
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      check=False)
  if proc.returncode != 0:
    raise RuntimeError("查询 malloc 汇总失败:\n" + proc.stdout)
  rows = parse_trace_processor_csv(
      proc.stdout,
      required_columns=("heap_name", "live_bytes", "allocated_bytes",
                        "freed_bytes"))
  heaps = []
  for row in rows:
    heaps.append({
        "heap_name": row.get("heap_name", ""),
        "live_bytes": int_value(row.get("live_bytes")),
        "allocated_bytes": int_value(row.get("allocated_bytes")),
        "freed_bytes": int_value(row.get("freed_bytes")),
    })
  return {
      "live_bytes": sum(item["live_bytes"] for item in heaps),
      "allocated_bytes": sum(item["allocated_bytes"] for item in heaps),
      "freed_bytes": sum(item["freed_bytes"] for item in heaps),
      "heaps": heaps,
  }


def query_mmap_validation_syscalls(trace_processor: str, trace_path: str, pid: int):
  """只查询目标进程 mmap/munmap/mremap syscall，不读取调用栈表。"""
  import mmap_phys_analyzer as analyzer

  sql = build_mmap_validation_syscalls_sql(pid)
  print("+ " + " ".join([trace_processor, "query", trace_path, sql]))
  proc = subprocess.run(
      [trace_processor, "query", trace_path, sql],
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      check=False)
  if proc.returncode != 0:
    raise RuntimeError("查询 mmap syscall 失败:\n" + proc.stdout)
  rows = parse_trace_processor_csv(
      proc.stdout,
      required_columns=("event_id", "ts", "utid", "event_name", "arg_id",
                        "key", "int_value", "string_value", "value_type"))
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
  JOIN __intrinsic_process pr ON th.upid = pr.id
  WHERE pr.pid = {pid}
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
  """一次 trace_processor 查询拿齐健康检查、malloc 累计汇总和 mmap syscall。"""
  quoted_names = ", ".join(f"'{name}'" for name in TRACE_HEALTH_STATS)
  return f"""
WITH
target_process AS (
  SELECT upid
  FROM process
  WHERE pid = {pid}
  ORDER BY start_ts DESC
  LIMIT 1
),
malloc_rows AS (
  SELECT
    h.heap_name AS heap_name,
    sum(h.size) AS live_bytes,
    sum(CASE WHEN h.size > 0 THEN h.size ELSE 0 END) AS allocated_bytes,
    sum(CASE WHEN h.size < 0 THEN -h.size ELSE 0 END) AS freed_bytes
  FROM heap_profile_allocation h
  JOIN target_process t ON h.upid = t.upid
  WHERE h.heap_name IN ('libc.malloc', 'malloc')
  GROUP BY h.heap_name
),
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
    0 AS sort_ts,
    0 AS sort_id,
    0 AS sort_arg,
    'malloc' AS section,
    heap_name AS c0,
    IFNULL(live_bytes, 0) AS c1,
    IFNULL(allocated_bytes, 0) AS c2,
    IFNULL(freed_bytes, 0) AS c3,
    '' AS c4,
    '' AS c5,
    '' AS c6,
    '' AS c7,
    '' AS c8,
    '' AS c9
  FROM malloc_rows
  UNION ALL
  SELECT
    2 AS sort_section,
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


def parse_malloc_summary_rows(rows):
  """把统一查询里的 malloc 行汇总成报告结构。"""
  heaps = []
  for row in rows:
    heaps.append({
        "heap_name": row.get("c0", ""),
        "live_bytes": int_value(row.get("c1")),
        "allocated_bytes": int_value(row.get("c2")),
        "freed_bytes": int_value(row.get("c3")),
    })
  return {
      "live_bytes": sum(item["live_bytes"] for item in heaps),
      "allocated_bytes": sum(item["allocated_bytes"] for item in heaps),
      "freed_bytes": sum(item["freed_bytes"] for item in heaps),
      "heaps": heaps,
  }


def query_memory_validation_inputs(trace_processor: str, trace_path: str, pid: int):
  """一次冷加载 trace，拿齐无栈健康和 malloc/native heap 口径校验输入。"""
  import mmap_phys_analyzer as analyzer

  sql = build_memory_validation_inputs_sql(pid)
  print("+ " + " ".join([trace_processor, "query", trace_path, sql]))
  proc = subprocess.run(
      [trace_processor, "query", trace_path, sql],
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
  health_rows = [
      {"name": row.get("c0", ""), "idx": row.get("c1", ""), "value": row.get("c2", "")}
      for row in rows
      if row.get("section") == "health"
  ]
  malloc_rows = [row for row in rows if row.get("section") == "malloc"]
  syscall_rows = [
      {
          "event_id": row.get("c0", ""),
          "ts": row.get("c1", ""),
          "utid": row.get("c2", ""),
          "event_name": row.get("c3", ""),
          "arg_id": row.get("c4", ""),
          "key": row.get("c5", ""),
          "int_value": row.get("c6", ""),
          "string_value": row.get("c7", ""),
          "value_type": row.get("c8", ""),
      }
      for row in rows
      if row.get("section") == "syscall" and all(
          row.get(key) not in ("", None)
          for key in ("c0", "c1", "c2", "c3", "c4", "c5"))
  ]
  return {
      "trace_health": summarize_trace_health(health_rows) if health_rows else None,
      "malloc_summary": parse_malloc_summary_rows(malloc_rows),
      "syscalls": build_syscall_events_from_rows(syscall_rows, analyzer),
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
  snapshots = analyzer.load_snapshots(smaps_dir, pid=pid, unit="auto", offset_ns=0)
  if not snapshots:
    return {"pss_bytes": 0, "rss_bytes": 0, "virtual_bytes": 0,
            "syscall_events": len(syscalls), "lifecycle_events": len(lifecycle)}

  ranges = []
  event_index = 0
  final_stats = {}
  for snapshot in snapshots:
    while event_index < len(lifecycle) and lifecycle[event_index][0] <= snapshot.ts:
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


def build_memory_validation_status(mmap_summary, trace_health,
                                   malloc_summary=None, meminfo=None):
  issues = []
  if int_value(mmap_summary.get("smaps_snapshots")) > 0 and int_value(
      mmap_summary.get("syscall_events")) == 0:
    issues.append("mmap_syscall_events_missing")
  if malloc_summary and meminfo:
    native_alloc = int_value(meminfo.get("native_heap_alloc_bytes"))
    malloc_live = int_value(malloc_summary.get("live_bytes"))
    if native_alloc > 0 and abs(malloc_live - native_alloc) / native_alloc > 0.5:
      issues.append("malloc_native_heap_alloc_mismatch")
  if trace_health:
    if int_value(trace_health.get("heapprofd_data_loss")) > 0:
      issues.append("heapprofd_data_loss")
    if int_value(trace_health.get("heapprofd_errors")) > 0:
      issues.append("heapprofd_errors")
    if int_value(trace_health.get("perfetto_data_loss")) > 0:
      issues.append("perfetto_data_loss")
    if int_value(trace_health.get("ftrace_data_loss")) > 0:
      issues.append("ftrace_data_loss")
    if int_value(trace_health.get("perf_data_loss")) > 0:
      issues.append("perf_data_loss")
  return {
      "status": "fail" if issues else "pass",
      "issues": issues,
  }


def has_heapprofd_health_issue(trace_health) -> bool:
  """判断 heapprofd 异常计数是否仍未清零。"""
  if not trace_health:
    return False
  return (
      int_value(trace_health.get("heapprofd_data_loss")) > 0 or
      int_value(trace_health.get("heapprofd_errors")) > 0)


def next_heapprofd_retry_params(sampling_interval_bytes: int,
                                shmem_size_bytes: int,
                                max_shmem_size_bytes: int,
                                max_sampling_interval_bytes: int):
  """生成下一轮 heapprofd 参数；先扩 shmem，再降低采样密度。"""
  if shmem_size_bytes < max_shmem_size_bytes:
    return (sampling_interval_bytes,
            min(shmem_size_bytes * 2, max_shmem_size_bytes))
  if sampling_interval_bytes < max_sampling_interval_bytes:
    return (min(sampling_interval_bytes * 2, max_sampling_interval_bytes),
            shmem_size_bytes)
  return None


def write_memory_validation_report(output_dir: str, malloc_summary,
                                   mmap_summary, meminfo_path: str,
                                   trace_health=None) -> str:
  with open(meminfo_path, "r", encoding="utf-8") as fd:
    meminfo = parse_meminfo_summary(fd.read())
  report = {
      "units": "bytes",
      "note": (
          "malloc 来自 heap_profile_allocation 全采集窗口累计净 live bytes；"
          "mmap 来自 mmap 归因最终 PSS；两者定义不同，不做合并校验。"),
      "malloc": malloc_summary,
      "mmap": mmap_summary,
      "trace_health": trace_health or {},
      "validation": build_memory_validation_status(
          mmap_summary, trace_health, malloc_summary, meminfo),
      "meminfo": meminfo,
      "comparison": {
          "malloc_live_minus_meminfo_native_heap_alloc_bytes":
              int_value(malloc_summary.get("live_bytes")) -
              meminfo["native_heap_alloc_bytes"],
      },
      "sources": {
          "meminfo": meminfo_path,
          "mmap": "mmap syscall events + smaps",
      },
  }
  path = os.path.join(output_dir, "memory_validation.json")
  with open(path, "w", encoding="utf-8") as fd:
    json.dump(report, fd, ensure_ascii=False, indent=2)
    fd.write("\n")
  print("内存验证:")
  print(f"  malloc live: {format_mib(report['malloc'].get('live_bytes', 0))}")
  print(f"  mmap PSS: {format_mib(report['mmap'].get('pss_bytes', 0))}")
  print(f"  meminfo Native Heap Alloc: "
        f"{format_mib(meminfo['native_heap_alloc_bytes'])}")
  print(f"  meminfo Native Heap PSS: "
        f"{format_mib(meminfo['native_heap_pss_bytes'])}")
  print(f"  validation status: {report['validation']['status']}")
  if report["validation"]["issues"]:
    print("  validation issues: " + ", ".join(report["validation"]["issues"]))
  print(f"验证报告已保存: {path}")
  return path


def collect_memory_validation(args, pid: int, trace_path: str, meminfo_path: str,
                              trace_health=None):
  malloc_summary = {"live_bytes": 0, "allocated_bytes": 0, "freed_bytes": 0,
                    "heaps": []}
  mmap_summary = {"pss_bytes": 0, "rss_bytes": 0, "virtual_bytes": 0}
  combined_inputs = None

  if args.trace_processor:
    try:
      combined_inputs = query_memory_validation_inputs(
          args.trace_processor, trace_path, pid)
    except RuntimeError as exc:
      print(str(exc), file=sys.stderr)

  if combined_inputs is not None:
    if trace_health is None:
      trace_health = combined_inputs.get("trace_health")
      if trace_health:
        print_trace_health(trace_health)
    if args.collect_malloc:
      malloc_summary = combined_inputs["malloc_summary"]
    mmap_summary = build_mmap_summary_from_syscalls(
        combined_inputs["syscalls"], pid, os.path.join(args.output, "smaps"))
  else:
    if args.collect_malloc:
      print("跳过 malloc 汇总：未指定 --trace-processor", file=sys.stderr)
    print("跳过 mmap 汇总：未指定 --trace-processor", file=sys.stderr)

  if not meminfo_path:
    print("跳过 meminfo 对比：采样结束后未成功保存 dumpsys meminfo", file=sys.stderr)
    return {"trace_health": trace_health, "report_path": ""}

  report_path = write_memory_validation_report(
      args.output, malloc_summary, mmap_summary, meminfo_path, trace_health)
  return {"trace_health": trace_health, "report_path": report_path}


def run_analyzer(args, pid: int, trace_path: str, smaps_dir: str):
  analyzer = args.analyzer
  if not analyzer:
    analyzer = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mmap_phys_analyzer.py")
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
      "--speedscope-output",
      os.path.join(args.output, "mmap_phys_attribution.speedscope.json"),
  ]
  if args.trace_processor:
    cmd.extend(["--trace-processor", args.trace_processor])
  subprocess.check_call(cmd)


def parse_args():
  parser = argparse.ArgumentParser(
      description="采集 mmap/perf/smaps 数据，并生成 mmap 物理内存归因结果")
  parser.add_argument("-n", "--name", required=True, help="目标进程名/包名")
  parser.add_argument("-d", "--duration-ms", type=int, default=75000,
                      help="Perfetto 采集时长，单位 ms")
  parser.add_argument("--smaps-interval-ms", type=int, default=1000,
                      help="smaps 采样间隔，单位 ms")
  parser.add_argument("-o", "--output", default=None, help="输出目录")
  parser.add_argument("--wait-timeout-s", type=int, default=120,
                      help="等待目标进程启动的超时时间；0 表示无限等待")
  parser.add_argument("--buffer-kb", type=int, default=262144,
                      help="Perfetto ring buffer 大小，单位 KiB")
  parser.add_argument("--perf-ring-buffer-pages", type=int, default=8192,
                      help="linux.perf 每 CPU ring buffer 页数；0 表示使用 Perfetto 默认值")
  parser.add_argument("--perf-ring-buffer-read-period-ms", type=int, default=100,
                      help="linux.perf ring buffer 读取周期；0 表示使用 Perfetto 默认值")
  parser.add_argument("--malloc", dest="collect_malloc", action="store_true",
                      default=True,
                      help="采集 libc.malloc 堆快照，用于 native heap 口径校验")
  parser.add_argument("--no-malloc", dest="collect_malloc", action="store_false",
                      help="不采集 libc.malloc 堆快照")
  parser.add_argument("--malloc-sampling-interval-bytes", type=int, default=4096,
                      help="heapprofd libc.malloc 采样间隔；1 表示尽量精确")
  parser.add_argument("--malloc-shmem-size-bytes", type=int, default=32 * 1024 * 1024,
                      help="heapprofd 共享内存大小，必须是 4096 的倍数且为 2 的幂")
  parser.add_argument("--no-auto-heapprofd-retry", dest="auto_heapprofd_retry",
                      action="store_false", default=True,
                      help="验证模式中 heapprofd 异常计数未清零时不自动调参重跑")
  parser.add_argument("--max-heapprofd-retries", type=int, default=10,
                      help="heapprofd 异常自动调参重试次数上限")
  parser.add_argument("--max-malloc-shmem-size-bytes", type=int,
                      default=512 * 1024 * 1024,
                      help="自动重试允许的 heapprofd shmem 上限")
  parser.add_argument("--max-malloc-sampling-interval-bytes", type=int,
                      default=65536,
                      help="自动重试允许的 malloc 采样间隔上限")
  parser.add_argument("--mmap-callstacks", dest="mmap_callstacks",
                      action="store_true", default=True,
                      help="额外采集 mmap 调用栈并运行 mmap 物理归因火焰图分析")
  parser.add_argument("--no-mmap-callstacks", dest="mmap_callstacks",
                      action="store_false",
                      help="不采集 mmap 调用栈；仅运行无栈健康和 malloc/native heap 口径校验时使用")
  parser.add_argument("--no-ftrace", action="store_true",
                      help="不启用 linux.ftrace syscall_events；会跳过无栈 mmap 验证")
  parser.add_argument("--no-kernel-frames", action="store_true",
                      help="mmap 调用栈不采内核帧")
  parser.add_argument("--no-guardrails", action="store_true",
                      help="传递给 perfetto 的 --no-guardrails")
  parser.add_argument("--use-su", action="store_true",
                      help="通过 su 0 读取 /proc/<pid>/smaps")
  parser.add_argument("--no-analyze", action="store_true",
                      help="只采集 trace 和 smaps，不运行离线分析器")
  parser.add_argument("--trace-processor", help="传给 mmap_phys_analyzer.py 的 trace_processor 路径")
  parser.add_argument("--analyzer", help="mmap_phys_analyzer.py 路径")
  return parser.parse_args()


def prepare_output_dir(output_dir: str) -> bool:
  os.makedirs(output_dir, exist_ok=True)
  if os.listdir(output_dir):
    print(f"FATAL: 输出目录非空: {output_dir}", file=sys.stderr)
    return False
  return True


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
      include_malloc=args.collect_malloc,
      malloc_sampling_interval_bytes=args.malloc_sampling_interval_bytes,
      malloc_shmem_size_bytes=args.malloc_shmem_size_bytes,
      include_mmap_callstacks=args.mmap_callstacks)
  write_config(config, args.output)
  if start_target_after_perfetto:
    # 验证 attempt 必须先让 heapprofd/ftrace 就绪，再启动 App，
    # 否则启动期 malloc/mmap 会被漏采，meminfo 对比没有意义。
    perfetto_pid = start_perfetto(config, device_trace, args.no_guardrails)
    pid = wait_for_pid(args.name, args.wait_timeout_s)
  else:
    pid = wait_for_pid(args.name, args.wait_timeout_s)
    perfetto_pid = start_perfetto(config, device_trace, args.no_guardrails)

  try:
    collect_smaps(pid, perfetto_pid, smaps_dir, args.smaps_interval_ms, args.use_su,
                  args.duration_ms, run_as_package=args.name)
  finally:
    if IS_INTERRUPTED:
      stop_perfetto(perfetto_pid)

  pull_trace(device_trace, trace_path)
  try:
    meminfo_path = capture_meminfo(args.name, args.output)
  except subprocess.CalledProcessError as exc:
    meminfo_path = ""
    print("跳过 meminfo 对比，dumpsys meminfo 失败:", file=sys.stderr)
    print(exc.output.decode("utf-8", errors="replace"), file=sys.stderr)

  trace_health = None
  if args.mmap_callstacks:
    trace_health = check_trace_health(args, trace_path)

  if args.mmap_callstacks and not args.no_analyze:
    run_analyzer(args, pid, trace_path, smaps_dir)
  elif not args.mmap_callstacks and not args.no_analyze:
    print("跳过 mmap 调用栈分析：当前验证模式未采集 mmap 调用栈")

  validation = collect_memory_validation(args, pid, trace_path, meminfo_path,
                                         trace_health)

  print("采集完成")
  print(f"输出目录: {os.path.abspath(args.output)}")
  validation = validation or {"trace_health": trace_health, "report_path": ""}
  validation["status"] = 0
  validation["output"] = args.output
  return validation


def should_auto_retry_heapprofd(args) -> bool:
  """仅无栈验证模式自动重跑，避免影响默认主功能采集。"""
  return (
      getattr(args, "auto_heapprofd_retry", True) and
      not args.mmap_callstacks and
      args.collect_malloc and
      bool(args.trace_processor))


def format_heapprofd_attempt_dir(base_output: str, attempt: int,
                                 sampling_interval_bytes: int,
                                 shmem_size_bytes: int) -> str:
  return os.path.join(
      base_output,
      f"attempt_{attempt:02d}_i{sampling_interval_bytes}_s{shmem_size_bytes}")


def run_auto_heapprofd_retry(args, base_output: str) -> int:
  """无栈验证模式下自动调参重跑，直到 heapprofd 异常计数清零。"""
  os.makedirs(base_output, exist_ok=True)
  if os.listdir(base_output):
    print(f"FATAL: 输出目录非空: {base_output}", file=sys.stderr)
    return 1

  attempt = 1
  while True:
    args.output = format_heapprofd_attempt_dir(
        base_output, attempt, args.malloc_sampling_interval_bytes,
        args.malloc_shmem_size_bytes)
    print("heapprofd 自动验证尝试:")
    print(f"  attempt={attempt}")
    print(f"  malloc_sampling_interval_bytes="
          f"{args.malloc_sampling_interval_bytes}")
    print(f"  malloc_shmem_size_bytes={args.malloc_shmem_size_bytes}")

    force_stop_app(args.name)
    result = run_collection(args, start_target_after_perfetto=True)
    if result.get("status", 0) != 0:
      return int(result.get("status", 1))

    trace_health = result.get("trace_health")
    if not has_heapprofd_health_issue(trace_health):
      print("heapprofd 自动验证通过：data_loss=0, errors=0")
      print(f"最终输出目录: {os.path.abspath(args.output)}")
      return 0

    if attempt > args.max_heapprofd_retries:
      print("FATAL: heapprofd 自动验证达到重试上限后仍有异常计数: "
            f"data_loss={trace_health.get('heapprofd_data_loss', 0)}, "
            f"errors={trace_health.get('heapprofd_errors', 0)}",
            file=sys.stderr)
      return 1

    next_params = next_heapprofd_retry_params(
        args.malloc_sampling_interval_bytes,
        args.malloc_shmem_size_bytes,
        args.max_malloc_shmem_size_bytes,
        args.max_malloc_sampling_interval_bytes)
    if next_params is None:
      print("FATAL: heapprofd 自动验证达到参数上限后仍有异常计数: "
            f"data_loss={trace_health.get('heapprofd_data_loss', 0)}, "
            f"errors={trace_health.get('heapprofd_errors', 0)}",
            file=sys.stderr)
      return 1

    args.malloc_sampling_interval_bytes, args.malloc_shmem_size_bytes = next_params
    print("WARN: heapprofd 异常仍存在，自动调整参数后重试: "
          f"--malloc-sampling-interval-bytes "
          f"{args.malloc_sampling_interval_bytes}, "
          f"--malloc-shmem-size-bytes {args.malloc_shmem_size_bytes}")
    attempt += 1


def main() -> int:
  signal.signal(signal.SIGINT, on_signal)
  signal.signal(signal.SIGTERM, on_signal)
  args = parse_args()

  if args.output is None:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    args.output = os.path.join("PerfData", "mmap_phys", stamp)

  if should_auto_retry_heapprofd(args):
    return run_auto_heapprofd_retry(args, args.output)

  return int(run_collection(args).get("status", 1))


if __name__ == "__main__":
  sys.exit(main())
