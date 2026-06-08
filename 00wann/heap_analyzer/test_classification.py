#!/usr/bin/env python3
"""classification.py 的单元测试。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import classification


class ClassificationTest(unittest.TestCase):

  def test_fs_ini_rules_classify_in_order_and_build_tree(self):
    """fs.ini 规则应顺序命中，父分类聚合所有子分类。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      config_path = os.path.join(tmpdir, "fs.ini")
      with open(config_path, "w", encoding="utf-8") as fd:
        fd.write("# il2cpp/meta\nClass::Init\n# il2cpp/audio\nAudioTrack\n")

      rules = classification.parse_classification_config(config_path)

    items = [
        {"id": 1, "stack": ("Leaf", "il2cpp::vm::Class::Init")},
        {"id": 2, "stack": ("Leaf", "android::AudioTrack")},
        {"id": 3, "stack": ("Leaf", "Other")},
    ]
    classified, remaining = classification.classify_items(
        items,
        rules,
        lambda item: item["stack"])

    self.assertEqual([rule.name for rule, _items in classified],
                     ["il2cpp/meta", "il2cpp/audio"])
    self.assertEqual([item.item["id"] for item in classified[0][1]], [1])
    self.assertEqual([item.item["id"] for item in classified[1][1]], [2])
    self.assertEqual([item.item["id"] for item in remaining], [3])

    entries = classification.build_hierarchy_entries(classified, remaining)
    entry_by_path = {entry.path: entry for entry in entries}
    self.assertEqual(
        [item.item["id"] for item in entry_by_path[("il2cpp",)].items],
        [1, 2])
    self.assertEqual(
        [item.item["id"] for item in entry_by_path[("remaining",)].items],
        [3])

  def test_first_matching_rule_wins_when_ui_and_hybridclr_keywords_overlap(self):
    """同时命中 UIManager 和 hybridclr 时，应按 fs.ini 顺序优先归入 UI。"""
    rules = [
        classification.ClassificationRule("fsui", ("UIManager",)),
        classification.ClassificationRule("hybridclr/other", ("hybridclr",)),
    ]
    items = [
        {
            "id": 1,
            "stack": (
                "Game.UI.UIManager::Open",
                "hybridclr::metadata::Image::Load",
                "Root",
            ),
        },
    ]

    classified, remaining = classification.classify_items(
        items,
        rules,
        lambda item: item["stack"])

    self.assertEqual([item.item["id"] for item in classified[0][1]], [1])
    self.assertEqual(classified[1][1], [])
    self.assertEqual(remaining, [])

  def test_sanitize_filename_keeps_portable_category_name(self):
    """分类名写文件前应替换路径和空白等不可移植字符。"""
    self.assertEqual(
        classification.sanitize_filename("il2cpp/meta class"),
        "il2cpp_meta_class")


if __name__ == "__main__":
  unittest.main()
