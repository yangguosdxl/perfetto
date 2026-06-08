#!/usr/bin/env python3
"""解析并校验 Android demo 的 dumpsys meminfo 输出。"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from typing import Dict, Iterable, List


@dataclasses.dataclass
class MemRow:
  """主表中的一行内存分类。"""

  pss: int = 0
  private_dirty: int = 0
  private_clean: int = 0
  swap_dirty: int = 0
  rss: int = 0
  heap_size: int = 0
  heap_alloc: int = 0
  heap_free: int = 0


@dataclasses.dataclass
class DatabaseRow:
  """DATABASES 中单连接统计行。"""

  page_size_kb: int
  db_size_kb: int
  lookaside_slots: int
  cache_hits: int
  cache_misses: int
  cache_size: int
  db_name: str


@dataclasses.dataclass
class ParsedMeminfo:
  """一次 dumpsys meminfo 快照。"""

  pid: int
  process_name: str
  table: Dict[str, MemRow]
  summary_pss: Dict[str, int]
  summary_rss: Dict[str, int]
  sql: Dict[str, int]
  databases: List[DatabaseRow]


@dataclasses.dataclass
class GrowthCheck:
  """前后两次快照的增长判定。"""

  name: str
  before: int
  after: int
  min_delta: int
  required: bool = True

  @property
  def delta(self) -> int:
    return self.after - self.before

  @property
  def passed(self) -> bool:
    return self.delta >= self.min_delta


MEMINFO_HEADER_RE = re.compile(r"\*\* MEMINFO in pid (\d+) \[([^\]]+)\] \*\*")
MAIN_ROW_RE = re.compile(
    r"^\s*(?P<label>[A-Za-z_. ][A-Za-z_. ]*?)\s+"
    r"(?P<pss>-?\d+)\s+"
    r"(?P<private_dirty>-?\d+)\s+"
    r"(?P<private_clean>-?\d+)\s+"
    r"(?P<swap_dirty>-?\d+)\s+"
    r"(?P<rss>-?\d+)"
    r"(?:\s+(?P<heap_size>-?\d+)\s+(?P<heap_alloc>-?\d+)\s+(?P<heap_free>-?\d+))?\s*$"
)
SUMMARY_RE = re.compile(
    r"^\s*(?P<label>[A-Za-z ]+):\s+(?P<pss>\d+)(?:\s+(?P<rss>\d+))?\s*$")
UNKNOWN_RSS_RE = re.compile(r"^\s*Unknown:\s+(?P<rss>\d+)\s*$")
SQL_ONE_RE = re.compile(r"MEMORY_USED:\s+(?P<memory>\d+)")
SQL_TWO_RE = re.compile(
    r"PAGECACHE_OVERFLOW:\s+(?P<overflow>\d+)\s+MALLOC_SIZE:\s+(?P<malloc>\d+)")
DATABASE_ROW_RE = re.compile(
    r"^\s*(?P<pgsz>\d+)\s+(?P<dbsz>\d+)\s+(?P<lookaside>\d+)\s+"
    r"(?P<hits>\d+)\s+(?P<misses>\d+)\s+(?P<cache>\d+)\s+(?P<name>/.+)$")


def _to_int(value: str | None) -> int:
  return int(value) if value is not None else 0


def parse_meminfo(text: str) -> ParsedMeminfo:
  """把 dumpsys meminfo 文本解析成结构化指标。"""
  pid = -1
  process_name = ""
  table: Dict[str, MemRow] = {}
  summary_pss: Dict[str, int] = {}
  summary_rss: Dict[str, int] = {}
  sql: Dict[str, int] = {}
  databases: List[DatabaseRow] = []

  in_app_summary = False
  in_databases = False

  for raw_line in text.splitlines():
    line = raw_line.rstrip()
    header_match = MEMINFO_HEADER_RE.search(line)
    if header_match:
      pid = int(header_match.group(1))
      process_name = header_match.group(2)
      continue

    if line.strip() == "App Summary":
      in_app_summary = True
      in_databases = False
      continue
    if line.strip() == "Objects":
      in_app_summary = False
      continue
    if line.strip() == "SQL":
      in_app_summary = False
      in_databases = False
      continue
    if line.strip() == "DATABASES":
      in_databases = True
      in_app_summary = False
      continue

    main_match = MAIN_ROW_RE.match(line)
    if main_match and main_match.group("label").strip() not in (
        "Pss", "Total", "Applications Memory Usage"):
      label = main_match.group("label").strip()
      if label not in ("Uptime", "Realtime"):
        table[label] = MemRow(
            pss=_to_int(main_match.group("pss")),
            private_dirty=_to_int(main_match.group("private_dirty")),
            private_clean=_to_int(main_match.group("private_clean")),
            swap_dirty=_to_int(main_match.group("swap_dirty")),
            rss=_to_int(main_match.group("rss")),
            heap_size=_to_int(main_match.group("heap_size")),
            heap_alloc=_to_int(main_match.group("heap_alloc")),
            heap_free=_to_int(main_match.group("heap_free")),
        )
      continue

    if in_app_summary:
      unknown_match = UNKNOWN_RSS_RE.match(line)
      if unknown_match:
        summary_rss["Unknown"] = int(unknown_match.group("rss"))
        continue
      summary_match = SUMMARY_RE.match(line)
      if summary_match:
        label = summary_match.group("label").strip()
        summary_pss[label] = int(summary_match.group("pss"))
        if summary_match.group("rss") is not None:
          summary_rss[label] = int(summary_match.group("rss"))
        continue

    sql_one = SQL_ONE_RE.search(line)
    if sql_one:
      sql["MEMORY_USED"] = int(sql_one.group("memory"))
      continue
    sql_two = SQL_TWO_RE.search(line)
    if sql_two:
      sql["PAGECACHE_OVERFLOW"] = int(sql_two.group("overflow"))
      sql["MALLOC_SIZE"] = int(sql_two.group("malloc"))
      continue

    if in_databases:
      db_match = DATABASE_ROW_RE.match(line)
      if db_match:
        databases.append(
            DatabaseRow(
                page_size_kb=int(db_match.group("pgsz")),
                db_size_kb=int(db_match.group("dbsz")),
                lookaside_slots=int(db_match.group("lookaside")),
                cache_hits=int(db_match.group("hits")),
                cache_misses=int(db_match.group("misses")),
                cache_size=int(db_match.group("cache")),
                db_name=db_match.group("name"),
            ))

  if pid < 0:
    raise ValueError("未找到 MEMINFO pid 头部")
  return ParsedMeminfo(
      pid=pid,
      process_name=process_name,
      table=table,
      summary_pss=summary_pss,
      summary_rss=summary_rss,
      sql=sql,
      databases=databases,
  )


def delta(after: ParsedMeminfo, before: ParsedMeminfo, row: str,
          field: str) -> int:
  """读取主表指定字段的前后差值。"""
  return getattr(after.table.get(row, MemRow()), field) - getattr(
      before.table.get(row, MemRow()), field)


def build_growth_checks(before: ParsedMeminfo,
                        after: ParsedMeminfo) -> List[GrowthCheck]:
  """构造 demo 预期增长项，阈值按 demo 默认分配量保守设置。"""
  before_sql_memory = before.sql.get("MEMORY_USED", 0)
  after_sql_memory = after.sql.get("MEMORY_USED", 0)
  before_db_size = sum(row.db_size_kb for row in before.databases)
  after_db_size = sum(row.db_size_kb for row in after.databases)
  return [
      GrowthCheck("Native Heap Private Dirty",
                  before.table.get("Native Heap", MemRow()).private_dirty,
                  after.table.get("Native Heap", MemRow()).private_dirty,
                  32 * 1024),
      GrowthCheck("Other mmap PSS",
                  before.table.get("Other mmap", MemRow()).pss,
                  after.table.get("Other mmap", MemRow()).pss, 8 * 1024),
      GrowthCheck("Unknown PSS",
                  before.table.get("Unknown", MemRow()).pss,
                  after.table.get("Unknown", MemRow()).pss, 16 * 1024),
      GrowthCheck("Graphics Summary PSS", before.summary_pss.get("Graphics", 0),
                  after.summary_pss.get("Graphics", 0), 16 * 1024, False),
      GrowthCheck("SQLite MEMORY_USED", before_sql_memory, after_sql_memory,
                  64),
      GrowthCheck("SQLite database size", before_db_size, after_db_size, 1024),
  ]


def format_checks(checks: Iterable[GrowthCheck]) -> str:
  """输出面向终端阅读的检查结果。"""
  lines = []
  for check in checks:
    if check.passed:
      status = "PASS"
    elif check.required:
      status = "FAIL"
    else:
      status = "WARN"
    lines.append(
        f"{status}: {check.name}: before={check.before}KB after={check.after}KB "
        f"delta={check.delta}KB min={check.min_delta}KB")
  return "\n".join(lines)


def _read(path: str) -> str:
  with open(path, "r", encoding="utf-8") as f:
    return f.read()


def main(argv: List[str]) -> int:
  parser = argparse.ArgumentParser(description="校验 Android meminfo demo 前后变化。")
  parser.add_argument(
      "--baseline", required=True, help="baseline dumpsys meminfo 文本")
  parser.add_argument("--after", required=True, help="after dumpsys meminfo 文本")
  args = parser.parse_args(argv)

  before = parse_meminfo(_read(args.baseline))
  after = parse_meminfo(_read(args.after))
  checks = build_growth_checks(before, after)
  print(format_checks(checks))
  return 0 if all(check.passed or not check.required for check in checks) else 1


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
