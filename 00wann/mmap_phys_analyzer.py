#!/usr/bin/env python3
"""
离线分析 mmap 真实物理内存占用，并输出 Perfetto UI 可加载的 Chrome JSON trace。

输入由两部分组成：
1. Perfetto trace：包含 raw_syscalls mmap/munmap/mremap 事件和 linux.perf 调用栈采样。
2. smaps 快照目录：每个文件是一份 /proc/<pid>/smaps 内容，文件名里带时间戳。

输出是 Chrome trace JSON。Perfetto UI 可以直接打开这个 JSON 文件。
"""

import argparse
import bisect
import csv
import io
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HEAP_ANALYZER_DIR = os.path.join(SCRIPT_DIR, "heap_analyzer")
if HEAP_ANALYZER_DIR not in sys.path:
  sys.path.insert(0, HEAP_ANALYZER_DIR)
import classification as common_classification

ARM64_MMAP_NR = 222
ARM64_MUNMAP_NR = 215
ARM64_MREMAP_NR = 216

ClassificationRule = common_classification.ClassificationRule
ClassifiedMmapItem = common_classification.ClassifiedItem

MMAP_CLASSIFICATION_METRICS = (
    "stack_count",
    "range_count",
    "pss_bytes",
    "rss_bytes",
    "virtual_bytes",
    "private_dirty_bytes",
    "private_clean_bytes",
    "shared_dirty_bytes",
    "shared_clean_bytes",
)


@dataclass
class Stack:
  id: int
  frames: List[str]

  @property
  def title(self) -> str:
    return self.frames[0] if self.frames else "<unknown>"

  @property
  def text(self) -> str:
    return "\n".join(self.frames) if self.frames else "<unknown>"


@dataclass
class PerfSample:
  ts: int
  utid: int
  pid: int
  tid: int
  callsite_id: int


@dataclass
class SyscallEvent:
  ts: int
  utid: int
  name: str
  syscall_id: Optional[int]
  ret: Optional[int]
  args: Dict[str, int]


@dataclass
class MmapRange:
  pid: int
  start: int
  end: int
  stack_id: int
  mmap_ts: int
  path: str = ""


@dataclass
class SmapsVma:
  start: int
  end: int
  pathname: str
  rss_kb: int = 0
  pss_kb: int = 0
  private_dirty_kb: int = 0
  private_clean_kb: int = 0
  shared_dirty_kb: int = 0
  shared_clean_kb: int = 0


@dataclass
class Snapshot:
  ts: int
  pid: int
  path: str
  vmas: List[SmapsVma]


@dataclass
class StackStat:
  stack_id: int
  virtual_bytes: int = 0
  rss_bytes: float = 0
  pss_bytes: float = 0
  private_dirty_bytes: float = 0
  private_clean_bytes: float = 0
  shared_dirty_bytes: float = 0
  shared_clean_bytes: float = 0
  range_count: int = 0
  paths: set = field(default_factory=set)


def run_tp_query(tp: str, trace: str, sql: str) -> List[Dict[str, str]]:
  """运行 trace_processor query，并从混合日志输出里提取 CSV 结果。"""
  proc = subprocess.run([tp, "query", trace, sql],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False)
  if proc.returncode != 0:
    raise RuntimeError((proc.stdout or "") + (proc.stderr or ""))

  csv_lines = []
  in_csv = False
  for line in proc.stdout.splitlines():
    if line.startswith('"') and '","' in line:
      in_csv = True
    if in_csv:
      if line.startswith("[") or line.startswith("Loading trace"):
        continue
      csv_lines.append(line)
  if not csv_lines:
    return []
  return list(csv.DictReader(io.StringIO("\n".join(csv_lines))))


def int_or_none(value: Optional[str]) -> Optional[int]:
  if value is None or value == "" or value == "[NULL]":
    return None
  try:
    return int(value, 0)
  except ValueError:
    return None


def first_int(args: Dict[str, int], names: Iterable[str]) -> Optional[int]:
  for name in names:
    if name in args:
      return args[name]
  return None


def load_perf_samples(tp: str, trace: str) -> List[PerfSample]:
  sql = """
  SELECT
    ps.ts AS ts,
    ps.utid AS utid,
    IFNULL(p.pid, 0) AS pid,
    IFNULL(t.tid, 0) AS tid,
    IFNULL(ps.callsite_id, -1) AS callsite_id
  FROM __intrinsic_perf_sample ps
  LEFT JOIN __intrinsic_thread t ON ps.utid = t.id
  LEFT JOIN __intrinsic_process p ON t.upid = p.id
  WHERE ps.callsite_id IS NOT NULL
  ORDER BY ps.ts
  """
  rows = run_tp_query(tp, trace, sql)
  return [
      PerfSample(
          ts=int(row["ts"]),
          utid=int(row["utid"]),
          pid=int(row["pid"]),
          tid=int(row["tid"]),
          callsite_id=int(row["callsite_id"])) for row in rows
  ]


def load_stack_symbol_names(tp: str, trace: str) -> Dict[int, List[str]]:
  """读取 frame.symbol_set_id 对应的 UI 符号名，失败时回退到 frame 名称。"""
  try:
    rows = run_tp_query(
        tp, trace, """
  SELECT symbol_set_id, id, name
  FROM stack_profile_symbol
  ORDER BY symbol_set_id, id
  """)
  except RuntimeError as exc:
    print(
        f"WARN: 读取 stack_profile_symbol 失败，回退到 frame 名称: {exc}",
        file=sys.stderr)
    return {}

  symbols: Dict[int, List[str]] = {}
  for row in rows:
    symbol_set_id = int_or_none(row.get("symbol_set_id"))
    name = row.get("name") or ""
    if symbol_set_id is None or not name:
      continue
    symbols.setdefault(symbol_set_id, []).append(name)
  return symbols


def load_stacks(tp: str, trace: str) -> Dict[int, Stack]:
  symbol_names = load_stack_symbol_names(tp, trace)
  callsite_rows = run_tp_query(
      tp, trace, """
  SELECT
    c.id AS id,
    IFNULL(c.parent_id, -1) AS parent_id,
    IFNULL(f.name, '') AS frame_name,
    IFNULL(f.deobfuscated_name, '') AS deobfuscated_name,
    IFNULL(f.symbol_set_id, -1) AS symbol_set_id,
    IFNULL(m.name, '') AS mapping_name,
    IFNULL(f.rel_pc, 0) AS rel_pc
  FROM __intrinsic_stack_profile_callsite c
  LEFT JOIN __intrinsic_stack_profile_frame f ON c.frame_id = f.id
  LEFT JOIN __intrinsic_stack_profile_mapping m ON f.mapping = m.id
  """)
  nodes = {}

  def with_mapping(name: str, mapping: str) -> str:
    if mapping:
      return f"{name} [{os.path.basename(mapping)}]"
    return name

  for row in callsite_rows:
    cid = int(row["id"])
    parent = int(row["parent_id"])
    symbol_set_id = int_or_none(row.get("symbol_set_id"))
    symbols = symbol_names.get(symbol_set_id or -1, [])
    mapping = row["mapping_name"]
    if symbols:
      frames = [with_mapping(symbol, mapping) for symbol in symbols]
    else:
      name = row["deobfuscated_name"] or row["frame_name"]
      rel_pc = int(row["rel_pc"] or 0)
      if not name:
        name = f"{mapping}+0x{rel_pc:x}" if mapping else f"0x{rel_pc:x}"
      else:
        name = with_mapping(name, mapping)
      frames = [name]
    nodes[cid] = (parent, frames)

  def build(cid: int) -> List[str]:
    frames = []
    seen = set()
    while cid in nodes and cid not in seen:
      seen.add(cid)
      parent, current_frames = nodes[cid]
      frames.extend(current_frames)
      cid = parent
    return frames

  return {cid: Stack(cid, build(cid)) for cid in nodes}


def build_syscalls_sql(pid: Optional[int] = None) -> str:
  if pid is not None:
    return f"""
  WITH
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
  interesting_events AS (
    SELECT event_id FROM named_syscall_events
    UNION
    SELECT event_id FROM raw_syscall_events
  )
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
  JOIN interesting_events ie ON tfe.event_id = ie.event_id
  JOIN __intrinsic_args a ON tfe.arg_set_id = a.arg_set_id
  ORDER BY tfe.ts, tfe.event_id, a.id
  """

  return f"""
  WITH interesting_events AS (
    SELECT DISTINCT fe.id AS event_id
    FROM __intrinsic_ftrace_event fe
    JOIN __intrinsic_args a ON fe.arg_set_id = a.arg_set_id
    WHERE
      fe.name LIKE '%mmap%' OR
      fe.name LIKE '%munmap%' OR
      fe.name LIKE '%mremap%' OR
      (
        (fe.name LIKE '%sys_enter%' OR fe.name LIKE '%sys_exit%') AND
        a.key IN ('id', 'syscall_nr', 'nr') AND
        a.int_value IN ({ARM64_MMAP_NR}, {ARM64_MUNMAP_NR}, {ARM64_MREMAP_NR})
      )
  )
  SELECT
    fe.id AS event_id,
    fe.ts AS ts,
    fe.utid AS utid,
    fe.name AS event_name,
    a.id AS arg_id,
    a.key AS key,
    IFNULL(a.int_value, 0) AS int_value,
    IFNULL(a.string_value, '') AS string_value,
    a.value_type AS value_type
  FROM __intrinsic_ftrace_event fe
  JOIN interesting_events ie ON fe.id = ie.event_id
  JOIN __intrinsic_args a ON fe.arg_set_id = a.arg_set_id
  ORDER BY fe.ts, fe.id, a.id
  """


def load_syscalls(tp: str,
                  trace: str,
                  pid: Optional[int] = None) -> List[SyscallEvent]:
  sql = build_syscalls_sql(pid)
  rows = run_tp_query(tp, trace, sql)
  grouped: Dict[int, SyscallEvent] = {}
  repeated_arg_count: Dict[int, int] = {}
  for row in rows:
    event_id = int(row["event_id"])
    ev = grouped.get(event_id)
    if ev is None:
      ev = SyscallEvent(
          ts=int(row["ts"]),
          utid=int(row["utid"]),
          name=row["event_name"],
          syscall_id=None,
          ret=None,
          args={})
      grouped[event_id] = ev

    key = row["key"]
    value = int_or_none(row["int_value"])
    if value is None:
      value = int_or_none(row["string_value"])
    if value is None:
      continue

    norm = normalize_arg_key(key)
    if norm == "args":
      index = repeated_arg_count.get(event_id, 0)
      repeated_arg_count[event_id] = index + 1
      norm = f"arg{index}"
    ev.args[norm] = value
    if norm in ("id", "syscall_nr", "nr"):
      ev.syscall_id = value
    if norm in ("ret", "retval", "return_value"):
      ev.ret = value

  return sorted(grouped.values(), key=lambda x: x.ts)


def normalize_arg_key(key: str) -> str:
  key = key.strip().lower()
  key = key.replace("raw_syscalls.", "")
  key = key.replace("sys_enter.", "")
  key = key.replace("sys_exit.", "")
  key = key.replace("common.", "")
  # Perfetto/ftrace 不同版本可能把 syscall 参数叫 args[1]、arg1 或 len。
  key = key.replace("args[", "arg").replace("]", "")
  aliases = {
      "a0": "arg0",
      "a1": "arg1",
      "a2": "arg2",
      "a3": "arg3",
      "a4": "arg4",
      "a5": "arg5",
      "addr": "arg0",
      "start": "arg0",
      "len": "arg1",
      "length": "arg1",
      "old_address": "arg0",
      "old_size": "arg1",
      "new_size": "arg2",
  }
  return aliases.get(key, key)


def is_enter(ev: SyscallEvent) -> bool:
  return "enter" in ev.name or ev.name.startswith("sys_")


def is_exit(ev: SyscallEvent) -> bool:
  return "exit" in ev.name


def syscall_kind(ev: SyscallEvent) -> Optional[str]:
  name = ev.name.lower()
  if "munmap" in name or ev.syscall_id == ARM64_MUNMAP_NR:
    return "munmap"
  if "mremap" in name or ev.syscall_id == ARM64_MREMAP_NR:
    return "mremap"
  if "mmap" in name or ev.syscall_id == ARM64_MMAP_NR:
    return "mmap"
  return None


def build_sample_index(
    samples: List[PerfSample]) -> Dict[int, Tuple[List[int], List[PerfSample]]]:
  by_utid: Dict[int, List[PerfSample]] = {}
  for sample in samples:
    by_utid.setdefault(sample.utid, []).append(sample)
  return {
      utid: ([sample.ts for sample in values], values)
      for utid, values in by_utid.items()
  }


def nearest_sample(index: Dict[int, Tuple[List[int], List[PerfSample]]],
                   utid: int, ts: int, window_ns: int) -> Optional[PerfSample]:
  item = index.get(utid)
  if not item:
    return None
  times, samples = item
  pos = bisect.bisect_left(times, ts)
  candidates = []
  if pos < len(samples):
    candidates.append(samples[pos])
  if pos > 0:
    candidates.append(samples[pos - 1])
  if not candidates:
    return None
  best = min(candidates, key=lambda sample: abs(sample.ts - ts))
  return best if abs(best.ts - ts) <= window_ns else None


def build_lifecycle_events(syscalls: List[SyscallEvent],
                           samples: List[PerfSample],
                           stack_window_ns: int) -> List[Tuple[int, str, dict]]:
  sample_index = build_sample_index(samples)
  pending: Dict[int, List[Tuple[str, SyscallEvent, Optional[PerfSample]]]] = {}
  events = []

  for ev in syscalls:
    kind = syscall_kind(ev)
    if kind is None:
      continue
    if is_enter(ev) and not is_exit(ev):
      sample = nearest_sample(sample_index, ev.utid, ev.ts, stack_window_ns)
      pending.setdefault(ev.utid, []).append((kind, ev, sample))
      continue
    if not is_exit(ev):
      continue

    stack = pending.get(ev.utid, [])
    if not stack:
      continue
    kind, enter, sample = stack.pop()
    if ev.ret is None or ev.ret < 0:
      continue

    if kind == "mmap":
      if sample is None or sample.callsite_id < 0:
        continue
      size = first_int(enter.args, ("arg1", "len", "length"))
      if size is None:
        size = 0
      if size < 0:
        continue
      events.append((ev.ts, "mmap", {
          "pid": sample.pid if sample else 0,
          "addr": ev.ret,
          "size": size,
          "stack_id": sample.callsite_id if sample else -1,
          "path": "",
      }))
    elif kind == "munmap":
      addr = first_int(enter.args, ("arg0", "addr", "start"))
      size = first_int(enter.args, ("arg1", "len", "length"))
      if addr is None or size is None or size <= 0:
        continue
      events.append((ev.ts, "munmap", {
          "pid": sample.pid if sample else 0,
          "addr": addr,
          "size": size,
      }))
    elif kind == "mremap":
      old_addr = first_int(enter.args, ("arg0", "old_address"))
      old_size = first_int(enter.args, ("arg1", "old_size"))
      new_size = first_int(enter.args, ("arg2", "new_size"))
      if old_addr is None or old_size is None or new_size is None:
        continue
      events.append((ev.ts, "mremap", {
          "pid": sample.pid if sample else 0,
          "old_addr": old_addr,
          "old_size": old_size,
          "new_addr": ev.ret,
          "new_size": new_size,
          "stack_id": sample.callsite_id if sample else -1,
      }))

  return sorted(events, key=lambda x: x[0])


HEADER_RE = re.compile(
    r"^([0-9a-fA-F]+)-([0-9a-fA-F]+)\s+\S+\s+\S+\s+\S+\s+\S+\s*(.*)$")
KV_RE = re.compile(r"^([A-Za-z_]+):\s+(\d+)\s+kB")


def parse_smaps(path: str) -> List[SmapsVma]:
  vmas: List[SmapsVma] = []
  current: Optional[SmapsVma] = None
  with open(path, "r", encoding="utf-8", errors="replace") as fd:
    for line in fd:
      line = line.rstrip("\n")
      match = HEADER_RE.match(line)
      if match:
        if current is not None:
          vmas.append(current)
        current = SmapsVma(
            start=int(match.group(1), 16),
            end=int(match.group(2), 16),
            pathname=match.group(3).strip())
        continue
      if current is None:
        continue
      kv = KV_RE.match(line)
      if not kv:
        continue
      key, value = kv.group(1), int(kv.group(2))
      if key == "Rss":
        current.rss_kb = value
      elif key == "Pss":
        current.pss_kb = value
      elif key == "Private_Dirty":
        current.private_dirty_kb = value
      elif key == "Private_Clean":
        current.private_clean_kb = value
      elif key == "Shared_Dirty":
        current.shared_dirty_kb = value
      elif key == "Shared_Clean":
        current.shared_clean_kb = value
  if current is not None:
    vmas.append(current)
  return vmas


def parse_timestamp_from_name(path: str, unit: str) -> int:
  name = os.path.basename(path)
  nums = re.findall(r"\d+", name)
  if not nums:
    raise ValueError(f"smaps 文件名缺少时间戳: {path}")
  raw = int(max(nums, key=len))
  if unit == "ns":
    return raw
  if unit == "us":
    return raw * 1000
  if unit == "ms":
    return raw * 1000 * 1000
  if unit == "s":
    return raw * 1000 * 1000 * 1000
  # auto：兼容 epoch 时间戳和 collect_smaps() 写入的设备 uptime 纳秒。
  # 14 位 uptime ns 如果误按 ms 放大，会超过 Perfetto int64 ns 范围，
  # Chrome JSON 导入时会溢出成负时间戳并触发 trace_sorter_negative_timestamp_dropped。
  digits = len(str(raw))
  if digits >= 18:
    return raw
  if digits >= 15:
    return raw * 1000
  if digits >= 12:
    as_ms_ns = raw * 1000 * 1000
    if as_ms_ns <= 9_000_000_000_000_000_000:
      return as_ms_ns
    return raw
  return raw * 1000 * 1000 * 1000


def load_snapshots(smaps_dir: str, pid: int, unit: str,
                   offset_ns: int) -> List[Snapshot]:
  snapshots = []
  for root, _, files in os.walk(smaps_dir):
    for file_name in files:
      path = os.path.join(root, file_name)
      try:
        ts = parse_timestamp_from_name(path, unit) + offset_ns
      except ValueError:
        continue
      vmas = parse_smaps(path)
      if vmas:
        snapshots.append(Snapshot(ts=ts, pid=pid, path=path, vmas=vmas))
  return sorted(snapshots, key=lambda x: x.ts)


def remove_overlap(ranges: List[MmapRange], pid: int, start: int,
                   size: int) -> List[MmapRange]:
  end = start + size
  result = []
  for r in ranges:
    if pid and r.pid and r.pid != pid:
      result.append(r)
      continue
    if end <= r.start or start >= r.end:
      result.append(r)
      continue
    if start > r.start:
      result.append(
          MmapRange(r.pid, r.start, start, r.stack_id, r.mmap_ts, r.path))
    if end < r.end:
      result.append(MmapRange(r.pid, end, r.end, r.stack_id, r.mmap_ts, r.path))
  return result


def apply_event(ranges: List[MmapRange], event: Tuple[int, str,
                                                      dict]) -> List[MmapRange]:
  _, kind, data = event
  if kind == "mmap":
    # MAP_FIXED 可能覆盖已有区间；先切掉重叠部分，再加入新映射。
    ranges = remove_overlap(ranges, data["pid"], data["addr"], data["size"])
    ranges.append(
        MmapRange(
            pid=data["pid"],
            start=data["addr"],
            end=data["addr"] + data["size"],
            stack_id=data["stack_id"],
            mmap_ts=event[0],
            path=data.get("path", "")))
  elif kind == "munmap":
    ranges = remove_overlap(ranges, data["pid"], data["addr"], data["size"])
  elif kind == "mremap":
    ranges = remove_overlap(ranges, data["pid"], data["old_addr"],
                            data["old_size"])
    if data["new_addr"] >= 0 and data["new_size"] > 0:
      ranges.append(
          MmapRange(
              pid=data["pid"],
              start=data["new_addr"],
              end=data["new_addr"] + data["new_size"],
              stack_id=data["stack_id"],
              mmap_ts=event[0]))
  return ranges


def overlap_size(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
  return max(0, min(a_end, b_end) - max(a_start, b_start))


def attribute_snapshot(snapshot: Snapshot,
                       ranges: List[MmapRange]) -> Dict[int, StackStat]:
  stats: Dict[int, StackStat] = {}
  finite_ranges = []
  point_ranges = []
  for r in ranges:
    if snapshot.pid and r.pid and snapshot.pid != r.pid:
      continue
    if r.end <= r.start:
      point_ranges.append(r)
    else:
      finite_ranges.append(r)

  # live range 理论上由 remove_overlap 维护为同 pid 不重叠区间；按地址排序后，
  # 每个 VMA 只需扫描它附近可能重叠的 range，避免 VMA 数 * range 数的全量比较。
  finite_ranges.sort(key=lambda r: (r.start, r.end))
  finite_starts = [r.start for r in finite_ranges]
  point_ranges.sort(key=lambda r: r.start)
  point_starts = [r.start for r in point_ranges]

  for vma in snapshot.vmas:
    vma_size = max(1, vma.end - vma.start)
    candidates: List[Tuple[MmapRange, int]] = []

    finite_pos = bisect.bisect_left(finite_starts, vma.end)
    index = finite_pos - 1
    while index >= 0:
      r = finite_ranges[index]
      if r.end <= vma.start:
        break
      overlap = overlap_size(vma.start, vma.end, r.start, r.end)
      if overlap:
        candidates.append((r, overlap))
      index -= 1

    # 部分设备的 raw_syscalls 只暴露 mmap 返回地址，不暴露 length。
    # 这种情况下用返回地址所在的当前 VMA 作为物理归因范围。
    point_begin = bisect.bisect_left(point_starts, vma.start)
    point_end = bisect.bisect_left(point_starts, vma.end)
    for r in point_ranges[point_begin:point_end]:
      candidates.append((r, vma_size))

    if not candidates:
      continue

    # 一个 smaps VMA 的 PSS/RSS 只能被归因一次。多个 mmap range 命中同一
    # VMA 时按 overlap 权重分摊，避免把同一份物理页重复计入多个调用栈。
    total_overlap = sum(overlap for _, overlap in candidates)
    denominator = max(vma_size, total_overlap)
    for r, overlap in candidates:
      ratio = overlap / denominator
      stat = stats.setdefault(r.stack_id, StackStat(r.stack_id))
      stat.virtual_bytes += vma_size * ratio
      stat.rss_bytes += vma.rss_kb * 1024 * ratio
      stat.pss_bytes += vma.pss_kb * 1024 * ratio
      stat.private_dirty_bytes += vma.private_dirty_kb * 1024 * ratio
      stat.private_clean_bytes += vma.private_clean_kb * 1024 * ratio
      stat.shared_dirty_bytes += vma.shared_dirty_kb * 1024 * ratio
      stat.shared_clean_bytes += vma.shared_clean_kb * 1024 * ratio
      stat.range_count += 1
      if vma.pathname:
        stat.paths.add(vma.pathname)
  return stats


def build_summary_items(
    stats: Iterable[StackStat],
    stacks: Dict[int, Stack],
    top_n: int,
) -> List[dict]:
  """把最终快照的调用栈统计转成 summary，top_n=0 表示全量。"""
  summary_items = []
  for stat in sorted(stats, key=lambda s: s.pss_bytes, reverse=True):
    if top_n > 0 and len(summary_items) >= top_n:
      break
    stack = stacks.get(stat.stack_id, Stack(stat.stack_id, ["<unknown>"]))
    summary_items.append({
        "stack_id": stat.stack_id,
        "pss_bytes": int(stat.pss_bytes),
        "rss_bytes": int(stat.rss_bytes),
        "virtual_bytes": int(stat.virtual_bytes),
        "private_dirty_bytes": int(stat.private_dirty_bytes),
        "private_clean_bytes": int(stat.private_clean_bytes),
        "shared_dirty_bytes": int(stat.shared_dirty_bytes),
        "shared_clean_bytes": int(stat.shared_clean_bytes),
        "range_count": stat.range_count,
        "paths": sorted(stat.paths),
        "stack": stack.frames,
        "stack_text": stack.text,
    })
  return summary_items


def build_chrome_trace(snapshots: List[Snapshot],
                       lifecycle_events: List[Tuple[int, str,
                                                    dict]], stacks: Dict[int,
                                                                         Stack],
                       top_n: int) -> Tuple[dict, List[dict], List[dict]]:
  trace_events = []
  pid = 9000
  final_stats: Dict[int, StackStat] = {}
  trace_events.append({
      "ph": "M",
      "name": "process_name",
      "pid": pid,
      "tid": 0,
      "args": {
          "name": "mmap physical attribution"
      }
  })

  ranges: List[MmapRange] = []
  event_index = 0
  tid_by_stack: Dict[int, int] = {}

  for snapshot in snapshots:
    while event_index < len(
        lifecycle_events) and lifecycle_events[event_index][0] <= snapshot.ts:
      ranges = apply_event(ranges, lifecycle_events[event_index])
      event_index += 1

    stats = attribute_snapshot(snapshot, ranges)
    final_stats = stats
    ranked = sorted(stats.values(), key=lambda s: s.pss_bytes, reverse=True)
    if top_n > 0:
      ranked = ranked[:top_n]

    total_pss = sum(s.pss_bytes for s in stats.values())
    total_rss = sum(s.rss_bytes for s in stats.values())
    trace_events.append({
        "ph": "C",
        "name": "total mmap PSS/RSS",
        "pid": pid,
        "tid": 0,
        "ts": snapshot.ts / 1000,
        "args": {
            "pss_bytes": int(total_pss),
            "rss_bytes": int(total_rss),
            "live_ranges": len(ranges),
        }
    })
    trace_events.append({
        "ph": "i",
        "s": "t",
        "name": "mmap snapshot details",
        "pid": pid,
        "tid": 0,
        "ts": snapshot.ts / 1000,
        "args": {
            "smaps": snapshot.path,
        }
    })

    for stat in ranked:
      if stat.stack_id not in tid_by_stack:
        tid_by_stack[stat.stack_id] = len(tid_by_stack) + 1
        stack = stacks.get(stat.stack_id, Stack(stat.stack_id, ["<unknown>"]))
        trace_events.append({
            "ph": "M",
            "name": "thread_name",
            "pid": pid,
            "tid": tid_by_stack[stat.stack_id],
            "args": {
                "name": stack.title[:80]
            }
        })

      stack = stacks.get(stat.stack_id, Stack(stat.stack_id, ["<unknown>"]))
      trace_events.append({
          "ph": "C",
          "name": "mmap stack PSS",
          "pid": pid,
          "tid": tid_by_stack[stat.stack_id],
          "ts": snapshot.ts / 1000,
          "args": {
              "pss_bytes": int(stat.pss_bytes),
              "rss_bytes": int(stat.rss_bytes),
              "virtual_bytes": int(stat.virtual_bytes),
              "private_dirty_bytes": int(stat.private_dirty_bytes),
              "private_clean_bytes": int(stat.private_clean_bytes),
              "shared_dirty_bytes": int(stat.shared_dirty_bytes),
              "shared_clean_bytes": int(stat.shared_clean_bytes),
              "range_count": stat.range_count,
          }
      })
      trace_events.append({
          "ph": "i",
          "s": "t",
          "name": "mmap stack details",
          "pid": pid,
          "tid": tid_by_stack[stat.stack_id],
          "ts": snapshot.ts / 1000,
          "args": {
              "stack_id": stat.stack_id,
              "paths": "; ".join(sorted(stat.paths)[:8]),
              "stack": stack.text,
          }
      })

  summary_items = build_summary_items(final_stats.values(), stacks, top_n)
  all_summary_items = build_summary_items(final_stats.values(), stacks, 0)

  return {
      "traceEvents": trace_events,
      "displayTimeUnit": "ns",
      "metadata": {
          "format":
              "mmap_phys_analyzer chrome trace",
          "description":
              "PSS/RSS attributed to mmap callstacks by smaps overlap",
          "final_summary":
              summary_items,
      }
  }, summary_items, all_summary_items


def build_speedscope(summary_items: List[dict],
                     metric: str = "pss_bytes",
                     name: str = "mmap PSS by callstack") -> dict:
  """把按调用栈聚合的内存结果转换为 speedscope sampled profile。"""
  frame_ids: Dict[str, int] = {}
  frames = []
  samples = []
  weights = []

  def frame_id(frame_name: str) -> int:
    if frame_name not in frame_ids:
      frame_ids[frame_name] = len(frames)
      frames.append({"name": frame_name})
    return frame_ids[frame_name]

  for item in summary_items:
    weight = int(item.get(metric, 0))
    if weight <= 0:
      continue
    # speedscope 栈顺序是 root -> leaf；内部 stack 记录是 leaf -> root。
    stack = list(reversed(item.get("stack", [])))
    if not stack:
      stack = ["<unknown>"]
    samples.append([frame_id(frame) for frame in stack])
    weights.append(weight)

  return {
      "$schema": "https://www.speedscope.app/file-format-schema.json",
      "shared": {
          "frames": frames,
      },
      "profiles": [{
          "type": "sampled",
          "name": name,
          "unit": "bytes",
          "startValue": 0,
          "endValue": sum(weights),
          "samples": samples,
          "weights": weights,
      }],
      "activeProfileIndex": 0,
      "exporter": "mmap_phys_analyzer.py",
  }


def parse_classification_config(path: str) -> List[ClassificationRule]:
  return common_classification.parse_classification_config(path)


def classify_mmap_summary_items(
    summary_items: List[dict],
    rules: List[ClassificationRule],
) -> Tuple[List[Tuple[ClassificationRule, List[ClassifiedMmapItem]]],
           List[ClassifiedMmapItem]]:
  """按 fs.ini 规则顺序分类 mmap 调用栈 summary。"""
  return common_classification.classify_items(
      summary_items, rules, lambda item: item.get("stack", ()))


def sum_mmap_summary_items(items: Iterable[dict]) -> Dict[str, int]:
  item_list = list(items)
  totals = {field: 0 for field in MMAP_CLASSIFICATION_METRICS}
  totals["stack_count"] = len(item_list)
  for item in item_list:
    for field in MMAP_CLASSIFICATION_METRICS:
      if field == "stack_count":
        continue
      totals[field] += int(item.get(field, 0))
  return totals


def add_mib_fields(values: Dict[str, Any]) -> Dict[str, Any]:
  """给字节指标补充 MiB 字段，方便在表格中直接查看。"""
  output = dict(values)
  for field in (
      "pss_bytes",
      "rss_bytes",
      "virtual_bytes",
      "private_dirty_bytes",
      "private_clean_bytes",
      "shared_dirty_bytes",
      "shared_clean_bytes",
  ):
    output[f"{field[:-6]}_mib"] = int(output.get(field, 0)) / 1048576.0
  return output


def build_mmap_classification_summary(
    summary_items: List[dict],
    classified: List[Tuple[ClassificationRule, List[ClassifiedMmapItem]]],
    remaining: List[ClassifiedMmapItem],
) -> Dict[str, Any]:
  categories = []
  classified_totals = {field: 0 for field in MMAP_CLASSIFICATION_METRICS}
  for index, (rule, items) in enumerate(classified, start=1):
    totals = sum_mmap_summary_items(item.item for item in items)
    for field in MMAP_CLASSIFICATION_METRICS:
      classified_totals[field] += totals[field]
    categories.append(
        add_mib_fields({
            "index": index,
            "name": rule.name,
            "keywords": list(rule.keywords),
            **totals,
        }))

  total = sum_mmap_summary_items(summary_items)
  remaining_total = sum_mmap_summary_items(item.item for item in remaining)
  return add_mib_fields({
      **total,
      "categories": categories,
      "remaining": add_mib_fields(remaining_total),
      "classified_total": add_mib_fields(classified_totals),
  })


def build_mmap_summary_hierarchy_entries(
    summary: Dict[str, Any],) -> List[Dict[str, Any]]:
  entries = common_classification.build_summary_hierarchy_entries(
      summary, MMAP_CLASSIFICATION_METRICS)
  return [add_mib_fields(entry) for entry in entries]


def write_mmap_classification_summary(path: str, summary: Dict[str,
                                                               Any]) -> None:
  remaining = summary["remaining"]
  classified_total = summary["classified_total"]
  byte_fields = [
      "pss_bytes",
      "rss_bytes",
      "virtual_bytes",
      "private_dirty_bytes",
      "private_clean_bytes",
      "shared_dirty_bytes",
      "shared_clean_bytes",
  ]
  summary_rows = [
      ["field", "value"],
      ["stack_count", summary["stack_count"]],
      ["range_count", summary["range_count"]],
  ]
  for field in byte_fields:
    summary_rows.append([field, summary[field]])
    summary_rows.append([f"{field[:-6]}_mib", summary[f"{field[:-6]}_mib"]])
  summary_rows.extend([
      ["classified_total_stack_count", classified_total["stack_count"]],
      ["classified_total_range_count", classified_total["range_count"]],
      ["classified_total_pss_bytes", classified_total["pss_bytes"]],
      ["classified_total_pss_mib", classified_total["pss_mib"]],
      ["remaining_stack_count", remaining["stack_count"]],
      ["remaining_range_count", remaining["range_count"]],
      ["remaining_pss_bytes", remaining["pss_bytes"]],
      ["remaining_pss_mib", remaining["pss_mib"]],
  ])

  hierarchy_entries = build_mmap_summary_hierarchy_entries(summary)
  max_depth = max((len(entry["path"]) for entry in hierarchy_entries),
                  default=1)
  category_rows: List[List[object]] = [[
      *[f"level_{index}" for index in range(1, max_depth + 1)],
      "full_name",
      "node_type",
      "keywords",
      "stack_count",
      "range_count",
      "pss_bytes",
      "pss_mib",
      "rss_bytes",
      "rss_mib",
      "virtual_bytes",
      "virtual_mib",
      "private_dirty_bytes",
      "private_dirty_mib",
      "private_clean_bytes",
      "private_clean_mib",
      "shared_dirty_bytes",
      "shared_dirty_mib",
      "shared_clean_bytes",
      "shared_clean_mib",
  ]]
  for entry in hierarchy_entries:
    entry_path = entry["path"]
    levels = list(entry_path) + [""] * (max_depth - len(entry_path))
    category_rows.append([
        *levels,
        "/".join(entry_path),
        "leaf" if entry["is_leaf"] else "group",
        "\n".join(entry["keywords"]),
        entry["stack_count"],
        entry["range_count"],
        entry["pss_bytes"],
        entry["pss_mib"],
        entry["rss_bytes"],
        entry["rss_mib"],
        entry["virtual_bytes"],
        entry["virtual_mib"],
        entry["private_dirty_bytes"],
        entry["private_dirty_mib"],
        entry["private_clean_bytes"],
        entry["private_clean_mib"],
        entry["shared_dirty_bytes"],
        entry["shared_dirty_mib"],
        entry["shared_clean_bytes"],
        entry["shared_clean_mib"],
    ])

  common_classification.write_xlsx(path, [("Summary", summary_rows),
                                          ("Tree", category_rows)])
  print(f"分类统计输出完成: {path}")


def write_mmap_summary_speedscope(
    output_path: str,
    summary: Dict[str, Any],
    metric: str = "pss_bytes",
) -> None:
  summary_items: List[dict] = []
  for entry in build_mmap_summary_hierarchy_entries(summary):
    if not entry["is_leaf"]:
      continue
    path = tuple(str(part) for part in entry["path"])
    if path == ("remaining",):
      stack = ["remaining", "mmap summary"]
    else:
      stack = [*reversed(path), "classified", "mmap summary"]
    summary_items.append({
        "stack": stack,
        metric: entry.get(metric, 0),
    })
  speedscope = build_speedscope(
      summary_items, metric=metric, name="mmap classification summary")
  common_classification.ensure_parent_dir(output_path)
  with open(output_path, "w", encoding="utf-8") as fd:
    json.dump(speedscope, fd, ensure_ascii=False)
  print(f"分类汇总火焰图输出完成: {output_path}")


def write_mmap_classification_speedscope_files(
    output_dir: str,
    classified: List[Tuple[ClassificationRule, List[ClassifiedMmapItem]]],
    remaining: List[ClassifiedMmapItem],
    metric: str = "pss_bytes",
) -> None:
  os.makedirs(output_dir, exist_ok=True)
  for index, entry in enumerate(
      common_classification.build_hierarchy_entries(classified, remaining),
      start=1):
    full_name = "/".join(entry.path)
    summary_items = [item.item for item in entry.items]
    output_path = os.path.join(
        output_dir,
        f"{index:02d}_{common_classification.sanitize_filename(full_name)}.speedscope.json"
    )
    speedscope = build_speedscope(
        summary_items, metric=metric, name=f"mmap category: {full_name}")
    with open(output_path, "w", encoding="utf-8") as fd:
      json.dump(speedscope, fd, ensure_ascii=False)
    print(f"分类火焰图输出完成: {output_path}")


def load_perfetto_root(config_dir: str) -> Optional[str]:
  config_path = os.path.join(config_dir, "config.sh")
  if not os.path.exists(config_path):
    return None
  with open(config_path, encoding="utf-8") as f:
    for line in f:
      for token in shlex.split(line, comments=True):
        if token == "export":
          continue
        if token.startswith("PerfettoRoot="):
          value = token.split("=", 1)[1]
          return os.path.abspath(os.path.join(config_dir, value))
  return None


def find_default_tp(config_dir: Optional[str] = None) -> Optional[str]:
  if config_dir is None:
    config_dir = os.path.dirname(os.path.abspath(__file__))
  perfetto_root = load_perfetto_root(config_dir)
  candidates = [
      "trace_processor_shell",
      "trace_processor",
  ]
  if perfetto_root:
    candidates.insert(
        0,
        os.path.join(perfetto_root,
                     "out/linux_clang_release/trace_processor_shell"))
  for candidate in candidates:
    if os.path.exists(candidate) and os.access(candidate, os.X_OK):
      return candidate
  return None


def default_analysis_output_dir(output_path: str) -> str:
  return os.path.dirname(os.path.abspath(output_path))


def resolve_analysis_output_path(output_path: str, path: Optional[str],
                                 default_name: str) -> str:
  if not path:
    return os.path.join(default_analysis_output_dir(output_path), default_name)
  if os.path.isabs(path):
    return path
  return os.path.join(default_analysis_output_dir(output_path), path)


def resolve_analysis_output_dir(output_path: str, path: str) -> str:
  if os.path.isabs(path):
    return path
  return os.path.join(default_analysis_output_dir(output_path), path)


def main() -> int:
  parser = argparse.ArgumentParser(
      description="按 mmap 调用栈归因 smaps PSS/RSS，并输出 Perfetto 可加载 JSON")
  parser.add_argument(
      "--trace", required=True, help="包含 mmap/perf 事件的 Perfetto trace")
  parser.add_argument("--smaps-dir", required=True, help="smaps 快照目录")
  parser.add_argument("--pid", type=int, required=True, help="目标进程 pid")
  parser.add_argument("--output", required=True, help="输出 Chrome JSON trace")
  parser.add_argument("--speedscope-output", help="额外输出 speedscope 火焰图 JSON")
  parser.add_argument("--classify-config", help="按 fs.ini 规则对 mmap 调用栈分类")
  parser.add_argument(
      "--classify-summary-out", help="分类统计 XLSX 输出路径；相对路径会写入 --output 同级目录")
  parser.add_argument(
      "--classify-summary-speedscope-out",
      help="分类统计 speedscope JSON 输出路径；相对路径会写入 --output 同级目录")
  parser.add_argument(
      "--classify-speedscope-dir",
      help="每个分类输出一个 speedscope JSON 的目录；相对路径会写入 --output 同级目录")
  parser.add_argument(
      "--trace-processor",
      default=find_default_tp(),
      help="trace_processor_shell 路径")
  parser.add_argument(
      "--smaps-ts-unit",
      choices=["auto", "ns", "us", "ms", "s"],
      default="auto")
  parser.add_argument(
      "--smaps-ts-offset-ns",
      type=int,
      default=0,
      help="smaps 时间戳到 trace 时间轴的偏移")
  parser.add_argument(
      "--stack-window-ns",
      type=int,
      default=5_000_000,
      help="mmap enter 与 perf sample 匹配窗口")
  parser.add_argument(
      "--top-n", type=int, default=50, help="每个快照输出 PSS 最大的 N 个调用栈；0 表示全部")
  args = parser.parse_args()

  if not args.trace_processor:
    print(
        "FATAL: 找不到 trace_processor_shell，请使用 --trace-processor 指定",
        file=sys.stderr)
    return 1

  print("加载 perf 调用栈采样...")
  samples = load_perf_samples(args.trace_processor, args.trace)
  print(f"perf samples: {len(samples)}")

  print("加载调用栈表...")
  stacks = load_stacks(args.trace_processor, args.trace)
  print(f"stacks: {len(stacks)}")

  print("加载 syscall 事件...")
  syscalls = load_syscalls(args.trace_processor, args.trace, pid=args.pid)
  print(f"syscall events: {len(syscalls)}")

  print("构建 mmap 生命周期...")
  lifecycle = build_lifecycle_events(syscalls, samples, args.stack_window_ns)
  print(f"lifecycle events: {len(lifecycle)}")

  print("加载 smaps 快照...")
  snapshots = load_snapshots(args.smaps_dir, args.pid, args.smaps_ts_unit,
                             args.smaps_ts_offset_ns)
  print(f"smaps snapshots: {len(snapshots)}")
  if not snapshots:
    print("FATAL: 没有可用 smaps 快照", file=sys.stderr)
    return 1

  print("生成 Perfetto 可加载 JSON...")
  output, summary_items, all_summary_items = build_chrome_trace(
      snapshots, lifecycle, stacks, args.top_n)
  with open(args.output, "w", encoding="utf-8") as fd:
    json.dump(output, fd, ensure_ascii=False)
  print(f"写入: {args.output}")

  if args.speedscope_output:
    speedscope = build_speedscope(summary_items)
    with open(args.speedscope_output, "w", encoding="utf-8") as fd:
      json.dump(speedscope, fd, ensure_ascii=False)
    print(f"写入火焰图: {args.speedscope_output}")

  if args.classify_config:
    print("生成 mmap 分类输出...")
    rules = parse_classification_config(args.classify_config)
    classified, remaining = classify_mmap_summary_items(all_summary_items,
                                                        rules)
    summary_data = build_mmap_classification_summary(all_summary_items,
                                                     classified, remaining)
    print(f"  config: {args.classify_config}")
    print(f"  rule_count: {len(rules)}")
    print(f"  source_top_n: 0")
    print(f"  stack_count: {summary_data['stack_count']}")
    print(f"  pss_bytes: {summary_data['pss_bytes']}")
    print(f"  pss_mib: {summary_data['pss_mib']:.3f}")
    for index, (rule, items) in enumerate(classified, start=1):
      totals = sum_mmap_summary_items(item.item for item in items)
      print(f"  #{index} {rule.name}")
      print(f"    keywords: {', '.join(rule.keywords)}")
      print(f"    stack_count: {totals['stack_count']}")
      print(f"    range_count: {totals['range_count']}")
      print(f"    pss_bytes: {totals['pss_bytes']}")
      print(f"    pss_mib: {totals['pss_bytes'] / 1048576.0:.3f}")
    remaining_total = sum_mmap_summary_items(item.item for item in remaining)
    print("  remaining")
    print(f"    stack_count: {remaining_total['stack_count']}")
    print(f"    range_count: {remaining_total['range_count']}")
    print(f"    pss_bytes: {remaining_total['pss_bytes']}")
    print(f"    pss_mib: {remaining_total['pss_bytes'] / 1048576.0:.3f}")

    summary_out = resolve_analysis_output_path(
        args.output, args.classify_summary_out,
        "mmap_classification_summary.xlsx")
    write_mmap_classification_summary(summary_out, summary_data)

    summary_speedscope_out = resolve_analysis_output_path(
        args.output, args.classify_summary_speedscope_out,
        "mmap_classification_summary.speedscope.json")
    write_mmap_summary_speedscope(summary_speedscope_out, summary_data)

    if args.classify_speedscope_dir:
      write_mmap_classification_speedscope_files(
          resolve_analysis_output_dir(args.output,
                                      args.classify_speedscope_dir), classified,
          remaining)
  return 0


if __name__ == "__main__":
  sys.exit(main())
