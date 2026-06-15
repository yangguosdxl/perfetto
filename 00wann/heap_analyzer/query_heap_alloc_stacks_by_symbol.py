#!/usr/bin/env python3
"""按符号查询 Perfetto Native heap 分配栈。

这个脚本专门绕开 trace_processor SQL 递归在大调用栈上的性能瓶颈：
trace_processor 只负责加载 trace 和导出基础表，调用栈图遍历在 Python 内存中完成。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import shlex
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable

import classification as common_classification

DEFAULT_TRACE = ("/home/dianhun/disk2/work/fsprofiler/PerfData/mem/"
                 "2026-06-01_18-57-13/symbolized-trace")


def load_perfetto_root(script_dir: str) -> str | None:
  config_dir = os.path.dirname(script_dir)
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
          if os.name == "nt" and value.startswith("/") and not value.startswith("//"):
            try:
              proc = subprocess.run(
                  ["cygpath", "-w", value],
                  text=True,
                  stdout=subprocess.PIPE,
                  stderr=subprocess.DEVNULL,
                  check=False)
              if proc.returncode == 0 and proc.stdout.strip():
                return os.path.abspath(proc.stdout.strip())
            except FileNotFoundError:
              pass
          return os.path.abspath(os.path.join(config_dir, value))
  return None


def default_trace_processor(script_dir: str | None = None) -> str:
  if script_dir is None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
  env_override = os.environ.get("TRACE_PROCESSOR")
  if env_override:
    return env_override
  perfetto_root = load_perfetto_root(script_dir)
  if perfetto_root:
    if os.name == "nt":
      candidates = [
          os.path.join(perfetto_root, "out", "win_clang",
                       "trace_processor_shell.exe"),
          os.path.join(perfetto_root, "out", "win",
                       "trace_processor_shell.exe"),
          os.path.join(perfetto_root, "out", "linux_clang_release",
                       "trace_processor_shell"),
      ]
    else:
      candidates = [
          os.path.join(perfetto_root, "out", "linux_clang_release",
                       "trace_processor_shell"),
          os.path.join(perfetto_root, "out", "android_arm64",
                       "trace_processor_shell"),
      ]
    for candidate in candidates:
      if os.path.exists(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return candidates[0]
  return "trace_processor_shell"


DEFAULT_TRACE_PROCESSOR = default_trace_processor()


@dataclass(frozen=True)
class Callsite:
  parent_id: int | None
  frame_id: int
  depth: int


@dataclass(frozen=True)
class Allocation:
  upid: int | None
  heap_name: str | None
  callsite_id: int
  net_alloc_count: int
  net_alloc_bytes: int


ClassificationRule = common_classification.ClassificationRule
ClassifiedAllocation = common_classification.ClassifiedItem
HierarchyEntry = common_classification.HierarchyEntry


def log(message: str) -> None:
  print(f"[heap-symbol-query] {message}", file=sys.stderr, flush=True)


def run_tp_query(trace_processor: str, trace: str, sql: str,
                 columns: list[str]) -> list[dict[str, str | None]]:
  """运行 trace_processor query，并解析输出中的 CSV 表。

  trace_processor 的日志在不同版本中可能混入 stdout/stderr，因此这里通过
  定位 CSV header 的方式提取结果表。
  """
  start = time.monotonic()
  result = subprocess.run(
      [trace_processor, "query", trace, sql],
      text=True,
      capture_output=True,
      check=False,
  )
  if result.stderr:
    print(result.stderr, file=sys.stderr, end="")
  if result.returncode != 0:
    if result.stdout:
      print(result.stdout, file=sys.stderr, end="")
    raise RuntimeError(f"trace_processor query 失败，退出码 {result.returncode}")

  lines = result.stdout.splitlines()
  header_index = None
  for index, line in enumerate(lines):
    try:
      parsed = next(csv.reader([line]))
    except csv.Error:
      continue
    if parsed == columns:
      header_index = index
      break
  if header_index is None:
    print(result.stdout, file=sys.stderr)
    raise RuntimeError(f"没有在 trace_processor 输出中找到 CSV header：{columns}")

  csv_lines: list[str] = []
  for line in lines[header_index:]:
    if not line:
      continue
    if line.startswith("[") or line.startswith("column "):
      continue
    try:
      parsed = next(csv.reader([line]))
    except csv.Error:
      continue
    if len(parsed) == len(columns):
      csv_lines.append(line)

  rows: list[dict[str, str | None]] = []
  for row in csv.DictReader(csv_lines):
    rows.append({
        key: (value if value != "[NULL]" else None)
        for key, value in row.items()
    })
  log(f"查询完成：{len(rows)} 行，用时 {time.monotonic() - start:.3f}s")
  return rows


def build_frame_labels(trace_processor: str, trace: str) -> dict[int, str]:
  log("读取 stack_profile_frame")
  frames = run_tp_query(
      trace_processor,
      trace,
      """
      SELECT id, name, deobfuscated_name, symbol_set_id
      FROM stack_profile_frame
      """,
      ["id", "name", "deobfuscated_name", "symbol_set_id"],
  )

  log("读取 stack_profile_symbol")
  symbols = run_tp_query(
      trace_processor,
      trace,
      """
      SELECT symbol_set_id, id, name
      FROM stack_profile_symbol
      ORDER BY symbol_set_id, id
      """,
      ["symbol_set_id", "id", "name"],
  )

  names_by_symbol_set: dict[int, list[str]] = defaultdict(list)
  for row in symbols:
    symbol_set_id = row["symbol_set_id"]
    name = row["name"]
    if symbol_set_id is not None and name:
      names_by_symbol_set[int(symbol_set_id)].append(name)

  labels: dict[int, str] = {}
  for row in frames:
    frame_id = row["id"]
    name = row["name"]
    deobfuscated_name = row["deobfuscated_name"]
    symbol_set_id = row["symbol_set_id"]
    label = None
    if symbol_set_id is not None:
      names = names_by_symbol_set.get(int(symbol_set_id), [])
      if names:
        label = "\n     [inline] ".join(names)
    if not label:
      label = deobfuscated_name or name or "[unknown]"
    labels[int(frame_id)] = label
  return labels


def build_callsites(
    trace_processor: str,
    trace: str) -> tuple[dict[int, Callsite], dict[int | None, list[int]]]:
  log("读取 stack_profile_callsite")
  rows = run_tp_query(
      trace_processor,
      trace,
      """
      SELECT id, parent_id, frame_id, depth
      FROM stack_profile_callsite
      """,
      ["id", "parent_id", "frame_id", "depth"],
  )

  callsites: dict[int, Callsite] = {}
  children: dict[int | None, list[int]] = defaultdict(list)
  for row in rows:
    callsite_id_raw = row["id"]
    parent_id_raw = row["parent_id"]
    frame_id = row["frame_id"]
    depth = row["depth"]
    callsite_id = int(callsite_id_raw)
    parent_id = int(parent_id_raw) if parent_id_raw is not None else None
    callsites[callsite_id] = Callsite(
        parent_id=parent_id,
        frame_id=int(frame_id),
        depth=int(depth),
    )
    children[parent_id].append(callsite_id)
  return callsites, children


def build_allocations(trace_processor: str, trace: str) -> list[Allocation]:
  log("读取并聚合 heap_profile_allocation")
  rows = run_tp_query(
      trace_processor,
      trace,
      """
      SELECT
        upid,
        heap_name,
        callsite_id,
        SUM(count) AS net_alloc_count,
        SUM(size) AS net_alloc_bytes
      FROM heap_profile_allocation
      WHERE callsite_id IS NOT NULL
      GROUP BY upid, heap_name, callsite_id
      """,
      [
          "upid", "heap_name", "callsite_id", "net_alloc_count",
          "net_alloc_bytes"
      ],
  )

  allocations: list[Allocation] = []
  for row in rows:
    upid = row["upid"]
    heap_name = row["heap_name"]
    callsite_id = row["callsite_id"]
    net_alloc_count = row["net_alloc_count"]
    net_alloc_bytes = row["net_alloc_bytes"]
    allocations.append(
        Allocation(
            upid=int(upid) if upid is not None else None,
            heap_name=heap_name,
            callsite_id=int(callsite_id),
            net_alloc_count=int(net_alloc_count or 0),
            net_alloc_bytes=int(net_alloc_bytes or 0),
        ))
  return allocations


def build_process_names(trace_processor: str,
                        trace: str) -> dict[int, tuple[int | None, str | None]]:
  log("读取 process")
  rows = run_tp_query(
      trace_processor,
      trace,
      """
      SELECT upid, pid, name
      FROM process
      """,
      ["upid", "pid", "name"],
  )
  processes: dict[int, tuple[int | None, str | None]] = {}
  for row in rows:
    upid = row["upid"]
    if upid is not None:
      pid = row["pid"]
      processes[int(upid)] = (int(pid) if pid is not None else None,
                              row["name"])
  return processes


def descendants(start_nodes: Iterable[int],
                children: dict[int | None, list[int]]) -> set[int]:
  matched: set[int] = set()
  queue = deque(start_nodes)
  while queue:
    callsite_id = queue.popleft()
    if callsite_id in matched:
      continue
    matched.add(callsite_id)
    queue.extend(children.get(callsite_id, ()))
  return matched


def parse_classification_config(path: str) -> list[ClassificationRule]:
  return common_classification.parse_classification_config(path)


def sanitize_filename(name: str) -> str:
  return common_classification.sanitize_filename(name)


def default_output_dir(trace: str) -> str:
  """返回 trace 同级的 heap_analyze 输出目录。"""
  return os.path.join(os.path.dirname(os.path.abspath(trace)), "heap_analyze")


def resolve_output_path(trace: str, path: str, default_name: str) -> str:
  """把相对输出路径解析到 trace 同级 heap_analyze 目录。"""
  output_dir = default_output_dir(trace)
  if not path:
    return os.path.join(output_dir, default_name)
  if os.path.isabs(path):
    return path
  return os.path.join(output_dir, path)


def resolve_output_dir(trace: str, path: str | None) -> str:
  """把相对输出目录解析到 trace 同级 heap_analyze 目录。"""
  output_dir = default_output_dir(trace)
  if not path:
    return output_dir
  if os.path.isabs(path):
    return path
  return os.path.join(output_dir, path)


def ensure_parent_dir(path: str) -> None:
  common_classification.ensure_parent_dir(path)


def remove_stale_generated_files(output_dir: str, suffix: str) -> None:
  """清理旧生成文件，避免分类规则顺序变化后残留旧编号结果。"""
  if not os.path.isdir(output_dir):
    return
  for name in os.listdir(output_dir):
    if not name.endswith(suffix):
      continue
    path = os.path.join(output_dir, name)
    if os.path.isfile(path):
      os.remove(path)


def is_explicit_arg(argv: list[str], name: str) -> bool:
  """判断用户是否显式传入某个 argparse 长参数。"""
  return any(arg == name or arg.startswith(f"{name}=") for arg in argv)


def should_use_all_allocations(args: argparse.Namespace,
                               explicit_symbol: bool) -> bool:
  """分类统计默认使用全量 allocation，除非用户显式指定 symbol。"""
  return bool(
      args.all_allocations or (args.classify_config and not explicit_symbol))


def normalize_output_paths(args: argparse.Namespace) -> str:
  """统一把输出路径归一化到 trace 同级 heap_analyze 目录。"""
  output_dir = default_output_dir(args.trace)
  if args.speedscope_out:
    args.speedscope_out = resolve_output_path(args.trace, args.speedscope_out,
                                              "native_heap.speedscope.json")
  if args.pprof_out is None:
    args.pprof_out = os.path.join(output_dir, "native_heap.pprof.pb.gz")
  elif args.pprof_out:
    args.pprof_out = resolve_output_path(args.trace, args.pprof_out,
                                         "native_heap.pprof.pb.gz")
  if args.classify_speedscope_dir:
    args.classify_speedscope_dir = resolve_output_dir(
        args.trace, args.classify_speedscope_dir)
  if args.classify_summary_out:
    args.classify_summary_out = resolve_output_path(args.trace,
                                                    args.classify_summary_out,
                                                    "summary.xlsx")
  if args.classify_summary_speedscope_out:
    args.classify_summary_speedscope_out = resolve_output_path(
        args.trace, args.classify_summary_speedscope_out,
        "summary.speedscope.json")
  return output_dir


def category_path(name: str) -> tuple[str, ...]:
  return common_classification.category_path(name)


def classify_allocations(
    allocations: list[Allocation],
    rules: list[ClassificationRule],
    callsites: dict[int, Callsite],
    frame_labels: dict[int, str],
) -> tuple[list[tuple[ClassificationRule, list[ClassifiedAllocation]]],
           list[ClassifiedAllocation]]:
  """按规则顺序分类，命中后从后续规则中过滤。

  每个 allocation 最多属于一个分类；没有命中的进入 remaining。
  """
  return common_classification.classify_items(
      allocations, rules, lambda allocation: stack_labels_leaf_to_root(
          allocation.callsite_id, callsites, frame_labels))


def build_hierarchy_entries(
    classified: list[tuple[ClassificationRule, list[ClassifiedAllocation]]],
    remaining: list[ClassifiedAllocation],
) -> list[HierarchyEntry]:
  """按分类名里的 / 生成树状节点。

  叶子节点对应 fs.ini 中的完整分类；父节点聚合所有子分类。因为分类阶段
  已经保证每个 allocation 只命中一个叶子分类，所以父节点可以直接累加子项。
  """
  return common_classification.build_hierarchy_entries(classified, remaining)


def sum_allocations(allocations: Iterable[Allocation]) -> tuple[int, int, int]:
  allocation_list = list(allocations)
  return (
      len(allocation_list),
      sum(alloc.net_alloc_count for alloc in allocation_list),
      sum(alloc.net_alloc_bytes for alloc in allocation_list),
  )


def format_stack(callsite_id: int, callsites: dict[int, Callsite],
                 frame_labels: dict[int, str]) -> str:
  return "\n  <- ".join(
      stack_labels_leaf_to_root(callsite_id, callsites, frame_labels))


def stack_labels_leaf_to_root(callsite_id: int, callsites: dict[int, Callsite],
                              frame_labels: dict[int, str]) -> list[str]:
  frames: list[str] = []
  current = callsite_id
  seen: set[int] = set()
  while current is not None and current not in seen:
    seen.add(current)
    callsite = callsites.get(current)
    if callsite is None:
      break
    frames.append(frame_labels.get(callsite.frame_id, "[unknown]"))
    current = callsite.parent_id
  return frames


def write_speedscope(
    output_path: str,
    profile_name: str,
    allocations: list[Allocation],
    callsites: dict[int, Callsite],
    frame_labels: dict[int, str],
    weight_mode: str,
    precomputed_stacks: dict[int, tuple[str, ...]] | None = None,
) -> None:
  """输出 speedscope sampled profile。

  speedscope 的 sampled profile 使用 root -> leaf 的 frame 索引数组；
  Native heap 的 net size 可能为负，因此默认只输出正向净分配。
  """
  frames: list[dict[str, str]] = []
  frame_ids: dict[str, int] = {}
  samples: list[list[int]] = []
  weights: list[int] = []

  def intern_frame(name: str) -> int:
    existing = frame_ids.get(name)
    if existing is not None:
      return existing
    frame_id = len(frames)
    frame_ids[name] = frame_id
    frames.append({"name": name})
    return frame_id

  skipped_non_positive = 0
  for alloc in allocations:
    if weight_mode == "absolute-net":
      weight = abs(alloc.net_alloc_bytes)
    else:
      weight = alloc.net_alloc_bytes
    if weight <= 0:
      skipped_non_positive += 1
      continue

    # Perfetto callsite 链是 leaf -> root；speedscope 栈按 root -> leaf 写入。
    leaf_to_root = (
        precomputed_stacks.get(alloc.callsite_id)
        if precomputed_stacks is not None else None)
    if leaf_to_root is None:
      leaf_to_root = tuple(
          stack_labels_leaf_to_root(alloc.callsite_id, callsites, frame_labels))
    stack = list(reversed(leaf_to_root))
    if not stack:
      continue
    samples.append([intern_frame(name) for name in stack])
    weights.append(weight)

  profile = {
      "type": "sampled",
      "name": profile_name,
      "unit": "bytes",
      "startValue": 0,
      "endValue": sum(weights),
      "samples": samples,
      "weights": weights,
  }
  data = {
      "$schema": "https://www.speedscope.app/file-format-schema.json",
      "shared": {
          "frames": frames,
      },
      "profiles": [profile],
      "activeProfileIndex": 0,
      "exporter": "heap_analyzer/query_heap_alloc_stacks_by_symbol.py",
      "name": profile_name,
  }

  ensure_parent_dir(output_path)
  with open(output_path, "w", encoding="utf-8") as output:
    json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
  log("speedscope 输出完成："
      f"{output_path}，samples={len(samples)}，frames={len(frames)}，"
      f"bytes={sum(weights)}，skipped_non_positive={skipped_non_positive}")


def write_classification_speedscope_files(
    output_dir: str,
    classified: list[tuple[ClassificationRule, list[ClassifiedAllocation]]],
    remaining: list[ClassifiedAllocation],
    callsites: dict[int, Callsite],
    frame_labels: dict[int, str],
    weight_mode: str,
) -> None:
  import os

  os.makedirs(output_dir, exist_ok=True)
  remove_stale_generated_files(output_dir, ".speedscope.json")
  for index, entry in enumerate(
      build_hierarchy_entries(classified, remaining), start=1):
    full_name = "/".join(entry.path)
    allocations = [item.item for item in entry.items]
    precomputed = {
        item.item.callsite_id: item.stack_leaf_to_root for item in entry.items
    }
    output_path = (
        f"{output_dir}/{index:02d}_{sanitize_filename(full_name)}.speedscope.json"
    )
    write_speedscope(
        output_path,
        f"Native heap category: {full_name}",
        allocations,
        callsites,
        frame_labels,
        weight_mode,
        precomputed,
    )


def write_summary_speedscope(
    output_path: str,
    summary: dict[str, object],
    weight_mode: str,
) -> None:
  """把分类 summary 输出成 speedscope sampled profile。

  这里每个 sample 对应一个叶子分类或 remaining。父分类不单独写 sample，
  而是通过 shared frame 路径自然聚合，避免父子重复计入。
  """
  frames: list[dict[str, str]] = []
  frame_ids: dict[str, int] = {}
  samples: list[list[int]] = []
  weights: list[int] = []

  def intern_frame(name: str) -> int:
    existing = frame_ids.get(name)
    if existing is not None:
      return existing
    frame_id = len(frames)
    frame_ids[name] = frame_id
    frames.append({"name": name})
    return frame_id

  skipped_non_positive = 0
  for entry in build_summary_hierarchy_entries(summary):
    if not entry["is_leaf"]:
      continue
    net_bytes = int(entry["net_alloc_bytes"])
    weight = abs(net_bytes) if weight_mode == "absolute-net" else net_bytes
    if weight <= 0:
      skipped_non_positive += 1
      continue

    path = tuple(str(part) for part in entry["path"])
    if path == ("remaining",):
      stack = ("Native heap summary", "remaining")
    else:
      stack = ("Native heap summary", "classified", *path)
    samples.append([intern_frame(name) for name in stack])
    weights.append(weight)

  profile_name = "Native heap classification summary"
  profile = {
      "type": "sampled",
      "name": profile_name,
      "unit": "bytes",
      "startValue": 0,
      "endValue": sum(weights),
      "samples": samples,
      "weights": weights,
  }
  data = {
      "$schema": "https://www.speedscope.app/file-format-schema.json",
      "shared": {
          "frames": frames,
      },
      "profiles": [profile],
      "activeProfileIndex": 0,
      "exporter": "heap_analyzer/query_heap_alloc_stacks_by_symbol.py",
      "name": profile_name,
  }

  ensure_parent_dir(output_path)
  with open(output_path, "w", encoding="utf-8") as output:
    json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
  log("summary speedscope 输出完成："
      f"{output_path}，samples={len(samples)}，frames={len(frames)}，"
      f"bytes={sum(weights)}，skipped_non_positive={skipped_non_positive}")


def _pb_varint(value: int) -> bytes:
  """编码 protobuf varint，负 int64 按二进制补码写入。"""
  if value < 0:
    value = (1 << 64) + value
  out = bytearray()
  while value >= 0x80:
    out.append((value & 0x7f) | 0x80)
    value >>= 7
  out.append(value)
  return bytes(out)


def _pb_key(field_number: int, wire_type: int) -> bytes:
  return _pb_varint((field_number << 3) | wire_type)


def _pb_int(field_number: int, value: int) -> bytes:
  return _pb_key(field_number, 0) + _pb_varint(value)


def _pb_bool(field_number: int, value: bool) -> bytes:
  return _pb_int(field_number, 1 if value else 0)


def _pb_message(field_number: int, payload: bytes) -> bytes:
  return _pb_key(field_number, 2) + _pb_varint(len(payload)) + payload


def _pb_string(field_number: int, value: str) -> bytes:
  payload = value.encode("utf-8")
  return _pb_key(field_number, 2) + _pb_varint(len(payload)) + payload


def _pprof_value_type(type_index: int, unit_index: int) -> bytes:
  return _pb_int(1, type_index) + _pb_int(2, unit_index)


def _pprof_line(function_id: int) -> bytes:
  return _pb_int(1, function_id)


def _pprof_mapping(filename_index: int) -> bytes:
  return (_pb_int(1, 1) + _pb_int(5, filename_index) + _pb_bool(7, True) +
          _pb_bool(8, True) + _pb_bool(9, True) + _pb_bool(10, True))


def _pprof_label(key_index: int, value_index: int) -> bytes:
  return _pb_int(1, key_index) + _pb_int(2, value_index)


def write_pprof(
    output_path: str,
    profile_name: str,
    allocations: list[Allocation],
    callsites: dict[int, Callsite],
    frame_labels: dict[int, str],
    processes: dict[int, tuple[int | None, str | None]] | None = None,
    extra_labels_by_callsite: dict[int, dict[str, str]] | None = None,
) -> None:
  """输出 pprof profile.pb.gz，便于同时查看火焰图和调用树。

  pprof sample 的 location_id 顺序是 leaf -> root，正好对应 Perfetto
  callsite 链展开后的顺序。
  """
  string_table = [""]
  string_ids = {"": 0}

  def intern(value: str | None) -> int:
    text = "" if value is None else str(value)
    existing = string_ids.get(text)
    if existing is not None:
      return existing
    string_ids[text] = len(string_table)
    string_table.append(text)
    return string_ids[text]

  sample_types = (
      ("positive_net_alloc_bytes", "bytes"),
      ("absolute_net_alloc_bytes", "bytes"),
      ("net_alloc_bytes", "bytes"),
      ("net_alloc_count", "count"),
  )
  profile = bytearray()
  for sample_type, unit in sample_types:
    profile += _pb_message(1,
                           _pprof_value_type(intern(sample_type), intern(unit)))

  mapping_filename_index = intern("native_heap")
  location_ids: dict[str, int] = {}
  function_payloads: list[bytes] = []
  location_payloads: list[bytes] = []

  def intern_location(frame_name: str) -> int:
    existing = location_ids.get(frame_name)
    if existing is not None:
      return existing
    location_id = len(location_ids) + 1
    location_ids[frame_name] = location_id
    name_index = intern(frame_name)
    function_payloads.append(
        _pb_int(1, location_id) + _pb_int(2, name_index) +
        _pb_int(3, name_index))
    location_payloads.append(
        _pb_int(1, location_id) + _pb_int(2, 1) +
        _pb_message(4, _pprof_line(location_id)))
    return location_id

  label_keys = {
      "heap_name": intern("heap_name"),
      "process_name": intern("process_name"),
      "pid": intern("pid"),
      "upid": intern("upid"),
      "callsite_id": intern("callsite_id"),
  }
  extra_labels_by_callsite = extra_labels_by_callsite or {}
  for labels in extra_labels_by_callsite.values():
    for key in labels:
      if key not in label_keys:
        label_keys[key] = intern(key)
  processes = processes or {}
  sample_count = 0
  for alloc in allocations:
    leaf_to_root = stack_labels_leaf_to_root(alloc.callsite_id, callsites,
                                             frame_labels)
    if not leaf_to_root:
      continue
    sample = bytearray()
    for frame_name in leaf_to_root:
      sample += _pb_int(1, intern_location(frame_name))
    sample += _pb_int(2, max(alloc.net_alloc_bytes, 0))
    sample += _pb_int(2, abs(alloc.net_alloc_bytes))
    sample += _pb_int(2, alloc.net_alloc_bytes)
    sample += _pb_int(2, alloc.net_alloc_count)
    pid, process_name = processes.get(alloc.upid, (None, None))
    labels = {
        "heap_name": alloc.heap_name,
        "process_name": process_name,
        "pid": pid,
        "upid": alloc.upid,
        "callsite_id": alloc.callsite_id,
    }
    labels.update(extra_labels_by_callsite.get(alloc.callsite_id, {}))
    for key, value in labels.items():
      if value is None:
        continue
      sample += _pb_message(3, _pprof_label(label_keys[key],
                                            intern(str(value))))
    profile += _pb_message(2, bytes(sample))
    sample_count += 1

  profile += _pb_message(3, _pprof_mapping(mapping_filename_index))
  for payload in location_payloads:
    profile += _pb_message(4, payload)
  for payload in function_payloads:
    profile += _pb_message(5, payload)
  profile_name_index = intern(profile_name)
  profile += _pb_message(
      11,
      _pprof_value_type(intern("positive_net_alloc_bytes"), intern("bytes")))
  profile += _pb_int(12, 1)
  profile += _pb_int(13, profile_name_index)
  profile += _pb_int(14, intern("positive_net_alloc_bytes"))

  for value in string_table:
    profile += _pb_string(6, value)

  ensure_parent_dir(output_path)
  with gzip.open(output_path, "wb") as output:
    output.write(bytes(profile))
  log("pprof 输出完成："
      f"{output_path}，samples={sample_count}，frames={len(location_ids)}")


def build_classification_label_map(
    classified: list[tuple[ClassificationRule, list[ClassifiedAllocation]]],
    remaining: list[ClassifiedAllocation],
) -> dict[int, dict[str, str]]:
  """生成 callsite_id -> pprof labels，用于在总 pprof 中按分类筛选。"""
  labels_by_callsite: dict[int, dict[str, str]] = {}
  for rule, items in classified:
    category = rule.name
    for item in items:
      labels_by_callsite[item.item.callsite_id] = {
          "category": category,
          "category_type": "classified",
      }
  for item in remaining:
    labels_by_callsite[item.item.callsite_id] = {
        "category": "remaining",
        "category_type": "remaining",
    }
  return labels_by_callsite


def write_classification_summary_pprof(
    output_path: str,
    summary: dict[str, object],
) -> None:
  """把分类 summary 输出成 pprof，便于用调用树查看分类层级。"""
  callsites: dict[int, Callsite] = {}
  frame_labels: dict[int, str] = {}
  allocations: list[Allocation] = []
  extra_labels: dict[int, dict[str, str]] = {}
  frame_ids: dict[str, int] = {}
  next_frame_id = 1
  next_callsite_id = 1

  def intern_frame(name: str) -> int:
    nonlocal next_frame_id
    existing = frame_ids.get(name)
    if existing is not None:
      return existing
    frame_id = next_frame_id
    next_frame_id += 1
    frame_ids[name] = frame_id
    frame_labels[frame_id] = name
    return frame_id

  def add_sample(
      root_to_leaf: tuple[str, ...],
      net_count: int,
      net_bytes: int,
      labels: dict[str, str],
  ) -> None:
    nonlocal next_callsite_id
    parent_id = None
    for depth, frame_name in enumerate(root_to_leaf):
      callsite_id = next_callsite_id
      next_callsite_id += 1
      callsites[callsite_id] = Callsite(
          parent_id=parent_id, frame_id=intern_frame(frame_name), depth=depth)
      parent_id = callsite_id
    if parent_id is None:
      return
    allocations.append(
        Allocation(
            upid=None,
            heap_name="classification",
            callsite_id=parent_id,
            net_alloc_count=net_count,
            net_alloc_bytes=net_bytes))
    extra_labels[parent_id] = labels

  for entry in build_summary_hierarchy_entries(summary):
    if not entry["is_leaf"]:
      continue
    path = tuple(str(part) for part in entry["path"])
    net_count = int(entry["net_alloc_count"])
    net_bytes = int(entry["net_alloc_bytes"])
    if path == ("remaining",):
      add_sample(("Native heap summary", "remaining"), net_count, net_bytes, {
          "category": "remaining",
          "category_type": "remaining"
      })
    else:
      category = "/".join(path)
      add_sample(("Native heap summary", "classified", *path), net_count,
                 net_bytes, {
                     "category": category,
                     "category_type": "classified"
                 })

  write_pprof(
      output_path,
      "Native heap classification summary",
      allocations,
      callsites,
      frame_labels,
      extra_labels_by_callsite=extra_labels)


def write_classification_pprof_files(
    output_dir: str,
    classified: list[tuple[ClassificationRule, list[ClassifiedAllocation]]],
    remaining: list[ClassifiedAllocation],
    callsites: dict[int, Callsite],
    frame_labels: dict[int, str],
    processes: dict[int, tuple[int | None, str | None]],
) -> None:
  """为分类树每个节点输出一个 pprof 明细文件。"""
  os.makedirs(output_dir, exist_ok=True)
  remove_stale_generated_files(output_dir, ".pprof.pb.gz")
  for index, entry in enumerate(
      build_hierarchy_entries(classified, remaining), start=1):
    full_name = "/".join(entry.path)
    allocations = [item.item for item in entry.items]
    labels = {
        item.item.callsite_id: {
            "category":
                full_name,
            "category_type":
                "remaining" if entry.path == ("remaining",) else "classified",
        } for item in entry.items
    }
    output_path = (
        f"{output_dir}/{index:02d}_{sanitize_filename(full_name)}.pprof.pb.gz")
    write_pprof(output_path, f"Native heap category: {full_name}", allocations,
                callsites, frame_labels, processes, labels)


def build_classification_summary(
    symbol: str,
    all_allocations: bool,
    matched_allocations: list[Allocation],
    matched_callsites: set[int],
    target_frames: set[int],
    target_callsites: list[int],
    classified: list[tuple[ClassificationRule, list[ClassifiedAllocation]]],
    remaining: list[ClassifiedAllocation],
) -> dict[str, object]:
  total_callsite_count, total_count, total_bytes = sum_allocations(
      matched_allocations)
  categories: list[dict[str, object]] = []
  classified_count = 0
  classified_bytes = 0
  for index, (rule, items) in enumerate(classified, start=1):
    callsite_count, net_count, net_bytes = sum_allocations(
        item.item for item in items)
    classified_count += net_count
    classified_bytes += net_bytes
    categories.append({
        "index": index,
        "name": rule.name,
        "keywords": list(rule.keywords),
        "matched_allocation_callsites": callsite_count,
        "net_alloc_count": net_count,
        "net_alloc_bytes": net_bytes,
        "net_alloc_mib": net_bytes / 1048576.0,
    })

  remaining_callsite_count, remaining_count, remaining_bytes = sum_allocations(
      item.item for item in remaining)
  return {
      "symbol": symbol,
      "all_allocations": all_allocations,
      "target_frames": len(target_frames),
      "target_callsites": len(target_callsites),
      "matched_callsites": len(matched_callsites),
      "matched_allocation_callsites": total_callsite_count,
      "net_alloc_count": total_count,
      "net_alloc_bytes": total_bytes,
      "net_alloc_mib": total_bytes / 1048576.0,
      "categories": categories,
      "remaining": {
          "matched_allocation_callsites": remaining_callsite_count,
          "net_alloc_count": remaining_count,
          "net_alloc_bytes": remaining_bytes,
          "net_alloc_mib": remaining_bytes / 1048576.0,
      },
      "classified_total": {
          "net_alloc_count": classified_count,
          "net_alloc_bytes": classified_bytes,
          "net_alloc_mib": classified_bytes / 1048576.0,
      },
  }


def build_summary_hierarchy_entries(
    summary: dict[str, object]) -> list[dict[str, object]]:
  entries = common_classification.build_summary_hierarchy_entries(
      summary,
      ("matched_allocation_callsites", "net_alloc_count", "net_alloc_bytes"))
  return [{
      **entry,
      "net_alloc_mib": int(entry["net_alloc_bytes"]) / 1048576.0,
  } for entry in entries]


def write_classification_summary(path: str, summary: dict[str, object]) -> None:
  categories = summary["categories"]
  remaining = summary["remaining"]
  classified_total = summary["classified_total"]

  summary_rows = [
      ["field", "value"],
      ["symbol", summary["symbol"]],
      ["all_allocations", summary["all_allocations"]],
      ["target_frames", summary["target_frames"]],
      ["target_callsites", summary["target_callsites"]],
      ["matched_callsites", summary["matched_callsites"]],
      ["matched_allocation_callsites", summary["matched_allocation_callsites"]],
      ["net_alloc_count", summary["net_alloc_count"]],
      ["net_alloc_bytes", summary["net_alloc_bytes"]],
      ["net_alloc_mib", summary["net_alloc_mib"]],
      ["classified_total_net_alloc_count", classified_total["net_alloc_count"]],
      ["classified_total_net_alloc_bytes", classified_total["net_alloc_bytes"]],
      ["classified_total_net_alloc_mib", classified_total["net_alloc_mib"]],
      [
          "remaining_matched_allocation_callsites",
          remaining["matched_allocation_callsites"]
      ],
      ["remaining_net_alloc_count", remaining["net_alloc_count"]],
      ["remaining_net_alloc_bytes", remaining["net_alloc_bytes"]],
      ["remaining_net_alloc_mib", remaining["net_alloc_mib"]],
  ]
  hierarchy_entries = build_summary_hierarchy_entries(summary)
  max_depth = max((len(entry["path"]) for entry in hierarchy_entries),
                  default=1)
  category_rows: list[list[object]] = [[
      *[f"level_{index}" for index in range(1, max_depth + 1)],
      "full_name",
      "node_type",
      "keywords",
      "matched_allocation_callsites",
      "net_alloc_count",
      "net_alloc_bytes",
      "net_alloc_mib",
  ]]
  for entry in hierarchy_entries:
    entry_path = entry["path"]
    levels = list(entry_path) + [""] * (max_depth - len(entry_path))
    category_rows.append([
        *levels,
        "/".join(entry_path),
        "leaf" if entry["is_leaf"] else "group",
        "\n".join(entry["keywords"]),
        entry["matched_allocation_callsites"],
        entry["net_alloc_count"],
        entry["net_alloc_bytes"],
        entry["net_alloc_mib"],
    ])

  common_classification.write_xlsx(path, [("Summary", summary_rows),
                                          ("Tree", category_rows)])
  log(f"分类统计输出完成：{path}")


def main() -> int:
  parser = argparse.ArgumentParser(description="查询调用栈中包含指定符号的 Native heap 分配栈。")
  parser.add_argument(
      "--trace", default=DEFAULT_TRACE, help="symbolized trace 路径")
  parser.add_argument(
      "--symbol",
      default="il2cpp::vm::Class::Init",
      help="需要在调用栈中匹配的符号子串；配合 --all-allocations 时忽略",
  )
  parser.add_argument(
      "--all-allocations",
      action="store_true",
      help="不过滤目标符号，直接分析全部 heap_profile_allocation 调用栈",
  )
  parser.add_argument(
      "--trace-processor",
      default=DEFAULT_TRACE_PROCESSOR,
      help="trace_processor 可执行文件路径",
  )
  parser.add_argument("--limit", type=int, default=50, help="输出的分配栈数量")
  parser.add_argument(
      "--speedscope-out",
      default=None,
      help="输出 speedscope JSON 文件路径；相对路径会写入 trace 同级 heap_analyze 目录",
  )
  parser.add_argument(
      "--pprof-out",
      nargs="?",
      const="native_heap.pprof.pb.gz",
      default=None,
      help="默认输出 pprof profile.pb.gz；不带路径时写入 trace 同级 heap_analyze/native_heap.pprof.pb.gz",
  )
  parser.add_argument(
      "--speedscope-weight",
      choices=("positive-net", "absolute-net"),
      default="positive-net",
      help="speedscope 样本权重：positive-net 只写正向净分配；absolute-net 使用净变化绝对值",
  )
  parser.add_argument(
      "--classify-config",
      default=None,
      help="按 fs.ini 规则对分配栈顺序分类",
  )
  parser.add_argument(
      "--classify-speedscope-dir",
      default=None,
      help="每个分类输出一个 speedscope JSON 的目录；相对路径会写入 trace 同级 heap_analyze 目录",
  )
  parser.add_argument(
      "--classify-summary-out",
      default=None,
      help="分类统计 XLSX 输出路径；相对路径会写入 trace 同级 heap_analyze 目录",
  )
  parser.add_argument(
      "--classify-summary-speedscope-out",
      default=None,
      help="分类统计 speedscope JSON 输出路径；相对路径会写入 trace 同级 heap_analyze 目录",
  )
  explicit_symbol = is_explicit_arg(sys.argv[1:], "--symbol")
  args = parser.parse_args()
  effective_all_allocations = should_use_all_allocations(args, explicit_symbol)
  output_dir = normalize_output_paths(args)

  total_start = time.monotonic()
  try:
    frame_labels = build_frame_labels(args.trace_processor, args.trace)
    callsites, children = build_callsites(args.trace_processor, args.trace)
    allocations = build_allocations(args.trace_processor, args.trace)
    processes = build_process_names(args.trace_processor, args.trace)

    if effective_all_allocations:
      target_frames = set()
      target_callsites = []
      matched_callsites = {alloc.callsite_id for alloc in allocations}
      matched_allocations = list(allocations)
    else:
      target_frames = {
          frame_id for frame_id, label in frame_labels.items()
          if args.symbol in label
      }
      target_callsites = [
          callsite_id for callsite_id, callsite in callsites.items()
          if callsite.frame_id in target_frames
      ]
      matched_callsites = descendants(target_callsites, children)
      matched_allocations = [
          alloc for alloc in allocations
          if alloc.callsite_id in matched_callsites
      ]
    matched_allocations.sort(
        key=lambda alloc: abs(alloc.net_alloc_bytes), reverse=True)

    total_count = sum(alloc.net_alloc_count for alloc in matched_allocations)
    total_bytes = sum(alloc.net_alloc_bytes for alloc in matched_allocations)

    print("summary")
    print(f"  symbol: {args.symbol}")
    print(f"  all_allocations: {effective_all_allocations}")
    print(f"  target_frames: {len(target_frames)}")
    print(f"  target_callsites: {len(target_callsites)}")
    print(f"  matched_callsites: {len(matched_callsites)}")
    print(f"  matched_allocation_callsites: {len(matched_allocations)}")
    print(f"  net_alloc_count: {total_count}")
    print(f"  net_alloc_bytes: {total_bytes}")
    print(f"  net_alloc_mib: {total_bytes / 1048576.0:.3f}")
    print(f"  output_dir: {output_dir}")
    print(f"  elapsed_seconds: {time.monotonic() - total_start:.3f}")
    if args.speedscope_out:
      print(f"  speedscope_out: {args.speedscope_out}")
    if args.pprof_out:
      print(f"  pprof_out: {args.pprof_out}")
    print()

    if args.speedscope_out:
      write_speedscope(
          args.speedscope_out,
          f"Native heap allocations containing {args.symbol}",
          matched_allocations,
          callsites,
          frame_labels,
          args.speedscope_weight,
      )
    if args.pprof_out and not args.classify_config:
      write_pprof(
          args.pprof_out,
          f"Native heap allocations containing {args.symbol}",
          matched_allocations,
          callsites,
          frame_labels,
          processes,
      )

    if args.classify_config:
      rules = parse_classification_config(args.classify_config)
      classified, remaining = classify_allocations(
          matched_allocations,
          rules,
          callsites,
          frame_labels,
      )
      summary_data = build_classification_summary(
          args.symbol,
          effective_all_allocations,
          matched_allocations,
          matched_callsites,
          target_frames,
          target_callsites,
          classified,
          remaining,
      )
      classified_total_count = 0
      classified_total_bytes = 0
      print("classification")
      print(f"  config: {args.classify_config}")
      print(f"  rule_count: {len(rules)}")
      for index, (rule, items) in enumerate(classified, start=1):
        callsite_count, net_count, net_bytes = sum_allocations(
            item.item for item in items)
        classified_total_count += net_count
        classified_total_bytes += net_bytes
        print(f"  #{index} {rule.name}")
        print(f"    keywords: {', '.join(rule.keywords)}")
        print(f"    matched_allocation_callsites: {callsite_count}")
        print(f"    net_alloc_count: {net_count}")
        print(f"    net_alloc_bytes: {net_bytes}")
        print(f"    net_alloc_mib: {net_bytes / 1048576.0:.3f}")
      remaining_callsite_count, remaining_count, remaining_bytes = sum_allocations(
          item.item for item in remaining)
      print("  remaining")
      print(f"    matched_allocation_callsites: {remaining_callsite_count}")
      print(f"    net_alloc_count: {remaining_count}")
      print(f"    net_alloc_bytes: {remaining_bytes}")
      print(f"    net_alloc_mib: {remaining_bytes / 1048576.0:.3f}")
      print("  classified_total")
      print(f"    net_alloc_count: {classified_total_count}")
      print(f"    net_alloc_bytes: {classified_total_bytes}")
      print(f"    net_alloc_mib: {classified_total_bytes / 1048576.0:.3f}")
      print()
      classification_labels = build_classification_label_map(
          classified, remaining)
      if args.pprof_out:
        write_pprof(
            args.pprof_out,
            f"Native heap allocations containing {args.symbol}",
            matched_allocations,
            callsites,
            frame_labels,
            processes,
            classification_labels,
        )
      category_summary_pprof_out = os.path.join(output_dir,
                                                "category_summary.pprof.pb.gz")
      write_classification_summary_pprof(
          category_summary_pprof_out,
          summary_data,
      )
      write_classification_pprof_files(
          os.path.join(output_dir, "pprof_categories"),
          classified,
          remaining,
          callsites,
          frame_labels,
          processes,
      )
      if args.classify_speedscope_dir:
        write_classification_speedscope_files(
            args.classify_speedscope_dir,
            classified,
            remaining,
            callsites,
            frame_labels,
            args.speedscope_weight,
        )
      summary_out = args.classify_summary_out
      if summary_out is None:
        summary_out = os.path.join(output_dir, "summary.xlsx")
      if summary_out:
        write_classification_summary(summary_out, summary_data)
      summary_speedscope_out = args.classify_summary_speedscope_out
      if summary_speedscope_out is None:
        summary_speedscope_out = os.path.join(output_dir,
                                              "summary.speedscope.json")
      if summary_speedscope_out:
        write_summary_speedscope(
            summary_speedscope_out,
            summary_data,
            args.speedscope_weight,
        )

    for index, alloc in enumerate(matched_allocations[:args.limit], start=1):
      pid, process_name = processes.get(alloc.upid, (None, None))
      print(f"#{index}")
      print(f"  upid: {alloc.upid}")
      print(f"  pid: {pid}")
      print(f"  process_name: {process_name}")
      print(f"  heap_name: {alloc.heap_name}")
      print(f"  callsite_id: {alloc.callsite_id}")
      print(f"  net_alloc_count: {alloc.net_alloc_count}")
      print(f"  net_alloc_bytes: {alloc.net_alloc_bytes}")
      print(f"  net_alloc_mib: {alloc.net_alloc_bytes / 1048576.0:.3f}")
      print("  stack:")
      for line in format_stack(alloc.callsite_id, callsites,
                               frame_labels).splitlines():
        print(f"    {line}")
      print()
  finally:
    pass

  return 0


if __name__ == "__main__":
  raise SystemExit(main())
