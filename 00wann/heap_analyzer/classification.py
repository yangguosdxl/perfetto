"""fs.ini 调用栈分类共用工具。"""

from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from xml.sax.saxutils import escape


@dataclass(frozen=True)
class ClassificationRule:
  name: str
  keywords: tuple[str, ...]


@dataclass(frozen=True)
class ClassifiedItem:
  item: Any
  stack_leaf_to_root: tuple[str, ...]


@dataclass(frozen=True)
class HierarchyEntry:
  path: tuple[str, ...]
  keywords: tuple[str, ...]
  items: tuple[ClassifiedItem, ...]
  is_leaf: bool


def parse_classification_config(path: str) -> list[ClassificationRule]:
  """解析 fs.ini 分类规则。

  规则格式：
    # 分类显示名称
    keyword1
    keyword2
  空行会被忽略；没有关键字的分类不会参与匹配。
  """
  rules: list[ClassificationRule] = []
  current_name: str | None = None
  current_keywords: list[str] = []

  def flush_current() -> None:
    nonlocal current_name, current_keywords
    if current_name is not None:
      keywords = tuple(keyword for keyword in current_keywords if keyword)
      if keywords:
        rules.append(ClassificationRule(current_name, keywords))
    current_name = None
    current_keywords = []

  with open(path, encoding="utf-8-sig") as config:
    for raw_line in config:
      line = raw_line.strip()
      if not line:
        continue
      if line.startswith("#"):
        flush_current()
        current_name = line[1:].strip()
        current_keywords = []
      elif current_name is not None:
        current_keywords.append(line)
      else:
        raise ValueError(f"分类关键字缺少分类名：{line}")
  flush_current()
  return rules


def sanitize_filename(name: str) -> str:
  sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
  return sanitized.strip("._") or "category"


def category_path(name: str) -> tuple[str, ...]:
  return tuple(part.strip() for part in name.split("/") if part.strip())


def classify_items(
    items: Iterable[Any],
    rules: list[ClassificationRule],
    stack_getter: Callable[[Any], Iterable[str]],
) -> tuple[list[tuple[ClassificationRule, list[ClassifiedItem]]],
           list[ClassifiedItem]]:
  """按规则顺序分类，命中后从后续规则中过滤。"""
  remaining: list[ClassifiedItem] = [
      ClassifiedItem(item, tuple(stack_getter(item))) for item in items
  ]

  classified: list[tuple[ClassificationRule, list[ClassifiedItem]]] = []
  for rule in rules:
    matched: list[ClassifiedItem] = []
    next_remaining: list[ClassifiedItem] = []
    for item in remaining:
      stack_text = "\n".join(item.stack_leaf_to_root)
      if any(keyword in stack_text for keyword in rule.keywords):
        matched.append(item)
      else:
        next_remaining.append(item)
    classified.append((rule, matched))
    remaining = next_remaining
  return classified, remaining


def build_hierarchy_entries(
    classified: list[tuple[ClassificationRule, list[ClassifiedItem]]],
    remaining: list[ClassifiedItem],
) -> list[HierarchyEntry]:
  """按分类名里的 / 生成树状节点，父节点聚合所有子分类。"""
  items_by_path: dict[tuple[str, ...], list[ClassifiedItem]] = {}
  keywords_by_leaf: dict[tuple[str, ...], tuple[str, ...]] = {}
  leaf_paths: set[tuple[str, ...]] = set()
  ordered_paths: list[tuple[str, ...]] = []

  def remember(path: tuple[str, ...]) -> None:
    if path not in items_by_path:
      ordered_paths.append(path)
      items_by_path[path] = []

  for rule, items in classified:
    path = category_path(rule.name)
    if not path:
      continue
    leaf_paths.add(path)
    keywords_by_leaf[path] = rule.keywords
    for depth in range(1, len(path) + 1):
      prefix = path[:depth]
      remember(prefix)
      items_by_path[prefix].extend(items)

  if remaining:
    remaining_path = ("remaining",)
    remember(remaining_path)
    leaf_paths.add(remaining_path)
    items_by_path[remaining_path].extend(remaining)

  return [
      HierarchyEntry(
          path=path,
          keywords=keywords_by_leaf.get(path, ()),
          items=tuple(items_by_path[path]),
          is_leaf=path in leaf_paths,
      ) for path in ordered_paths
  ]


def build_summary_hierarchy_entries(
    summary: dict[str, Any],
    metric_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
  """把 summary.categories 和 remaining 展开为可写表格/火焰图的层级节点。"""
  items_by_path: dict[tuple[str, ...], dict[str, Any]] = {}
  leaf_paths: set[tuple[str, ...]] = set()
  ordered_paths: list[tuple[str, ...]] = []

  def remember(path: tuple[str, ...]) -> dict[str, Any]:
    existing = items_by_path.get(path)
    if existing is not None:
      return existing
    ordered_paths.append(path)
    item = {"path": path, "keywords": []}
    for field in metric_fields:
      item[field] = 0
    items_by_path[path] = item
    return item

  for category in summary["categories"]:
    path = category_path(str(category["name"]))
    if not path:
      continue
    leaf_paths.add(path)
    for depth in range(1, len(path) + 1):
      item = remember(path[:depth])
      for field in metric_fields:
        item[field] += category.get(field, 0)
    items_by_path[path]["keywords"] = list(category["keywords"])

  remaining_path = ("remaining",)
  leaf_paths.add(remaining_path)
  remaining_item = remember(remaining_path)
  remaining = summary["remaining"]
  for field in metric_fields:
    remaining_item[field] = remaining.get(field, 0)

  return [{
      **items_by_path[path],
      "is_leaf": path in leaf_paths,
  } for path in ordered_paths]


def ensure_parent_dir(path: str) -> None:
  parent = os.path.dirname(os.path.abspath(path))
  if parent:
    os.makedirs(parent, exist_ok=True)


def excel_column_name(index: int) -> str:
  name = ""
  while index:
    index, remainder = divmod(index - 1, 26)
    name = chr(ord("A") + remainder) + name
  return name


def xlsx_cell(row_index: int, column_index: int, value: object) -> str:
  ref = f"{excel_column_name(column_index)}{row_index}"
  if value is None:
    return f'<c r="{ref}"/>'
  if isinstance(value, bool):
    text = "TRUE" if value else "FALSE"
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'
  if isinstance(value, (int, float)) and not isinstance(value, bool):
    return f'<c r="{ref}"><v>{value}</v></c>'
  text = escape(str(value))
  return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def xlsx_sheet(rows: list[list[object]]) -> str:
  row_xml = []
  for row_index, row in enumerate(rows, start=1):
    cells = [
        xlsx_cell(row_index, column_index, value)
        for column_index, value in enumerate(row, start=1)
    ]
    row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
  return (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
      '<sheetData>'
      f'{"".join(row_xml)}'
      '</sheetData>'
      '</worksheet>')


def write_xlsx(path: str, sheets: list[tuple[str, list[list[object]]]]) -> None:
  workbook_sheets = []
  workbook_rels = []
  overrides = [
      '<Override PartName="/xl/workbook.xml" '
      'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
      '<Override PartName="/xl/styles.xml" '
      'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
  ]
  for index, (name, _rows) in enumerate(sheets, start=1):
    safe_name = escape(name[:31])
    workbook_sheets.append(
        f'<sheet name="{safe_name}" sheetId="{index}" r:id="rId{index}"/>')
    workbook_rels.append(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>')
    overrides.append(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    )
  styles_rid = len(sheets) + 1
  workbook_rels.append(
      f'<Relationship Id="rId{styles_rid}" '
      'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
      'Target="styles.xml"/>')

  content_types = (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>'
      f'{"".join(overrides)}'
      '</Types>')
  root_rels = (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
      '<Relationship Id="rId1" '
      'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
      'Target="xl/workbook.xml"/>'
      '</Relationships>')
  workbook = (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
      '<sheets>'
      f'{"".join(workbook_sheets)}'
      '</sheets>'
      '</workbook>')
  rels = (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
      f'{"".join(workbook_rels)}'
      '</Relationships>')
  styles = (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
      '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
      '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
      '<borders count="1"><border/></borders>'
      '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
      '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
      '</styleSheet>')

  ensure_parent_dir(path)
  with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("[Content_Types].xml", content_types)
    archive.writestr("_rels/.rels", root_rels)
    archive.writestr("xl/workbook.xml", workbook)
    archive.writestr("xl/_rels/workbook.xml.rels", rels)
    archive.writestr("xl/styles.xml", styles)
    for index, (_name, rows) in enumerate(sheets, start=1):
      archive.writestr(f"xl/worksheets/sheet{index}.xml", xlsx_sheet(rows))
