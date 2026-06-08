#!/usr/bin/env python3
"""query_heap_alloc_stacks_by_symbol.py 的单元测试。"""

import os
import subprocess
import tempfile
import unittest

import query_heap_alloc_stacks_by_symbol as analyzer


class HeapAllocStacksBySymbolTest(unittest.TestCase):

  def test_default_trace_processor_uses_perfetto_root_from_config(self):
    """默认 trace_processor 应从上级 config.sh 的 PerfettoRoot 推导。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      script_dir = os.path.join(tmpdir, "heap_analyzer")
      os.makedirs(script_dir)
      with open(os.path.join(tmpdir, "config.sh"), "w", encoding="utf-8") as f:
        f.write("export PerfettoRoot='perfetto-root'\n")

      expected = os.path.abspath(
          os.path.join(tmpdir, "perfetto-root/out/linux_clang_release/trace_processor_shell"))

      self.assertEqual(analyzer.default_trace_processor(script_dir), expected)

  def test_default_output_dir_is_trace_sibling_heap_analyze(self):
    """默认分析结果目录应位于 symbolized-trace 同级 heap_analyze。"""
    trace = "/tmp/perfetto-case/symbolized-trace"

    output_dir = analyzer.default_output_dir(trace)

    self.assertEqual(output_dir, "/tmp/perfetto-case/heap_analyze")


  def test_classify_config_defaults_to_all_allocations_without_explicit_symbol(self):
    """分类统计默认应覆盖全部 allocation，而不是被默认 symbol 过滤。"""
    class Args:
      all_allocations = False
      classify_config = "heap_analyzer/fs.ini"

    self.assertTrue(
        analyzer.should_use_all_allocations(Args(), explicit_symbol=False))
    self.assertFalse(
        analyzer.should_use_all_allocations(Args(), explicit_symbol=True))

  def test_default_outputs_enable_pprof_under_trace_heap_analyze(self):
    """默认输出应启用 pprof，并写入 trace 同级 heap_analyze。"""
    class Args:
      trace = "/tmp/perfetto-case/symbolized-trace"
      speedscope_out = None
      pprof_out = None
      classify_speedscope_dir = None
      classify_summary_out = None
      classify_summary_speedscope_out = None

    args = Args()

    analyzer.normalize_output_paths(args)

    self.assertEqual(
        args.pprof_out,
        "/tmp/perfetto-case/heap_analyze/native_heap.pprof.pb.gz")


  def test_write_pprof_keeps_classification_category_labels(self):
    """总 pprof 在分类模式下应保留 category 标签。"""
    callsites = {
        1: analyzer.Callsite(parent_id=None, frame_id=10, depth=0),
        2: analyzer.Callsite(parent_id=1, frame_id=20, depth=1),
    }
    frame_labels = {10: "Root", 20: "Allocator"}
    allocations = [analyzer.Allocation(7, "libc.malloc", 2, 3, 4096)]

    with tempfile.TemporaryDirectory() as tmpdir:
      output_path = os.path.join(tmpdir, "native_heap.pprof.pb.gz")
      analyzer.write_pprof(
          output_path,
          "Native heap test",
          allocations,
          callsites,
          frame_labels,
          processes={7: (1234, "game")},
          extra_labels_by_callsite={2: {"category": "il2cpp/meta"}})

      result = subprocess.run(
          ["go", "tool", "pprof", "-raw", output_path],
          text=True,
          capture_output=True,
          check=False)

    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("category:[il2cpp/meta]", result.stdout)

  def test_write_classification_summary_pprof_is_readable(self):
    """分类 summary pprof 应把叶子分类写成可读调用栈。"""
    summary = {
        "categories": [
            {
                "name": "il2cpp/meta",
                "keywords": ["Class::Init"],
                "matched_allocation_callsites": 2,
                "net_alloc_count": 3,
                "net_alloc_bytes": 4096,
                "net_alloc_mib": 4096 / 1048576.0,
            }
        ],
        "remaining": {
            "matched_allocation_callsites": 1,
            "net_alloc_count": -1,
            "net_alloc_bytes": -1024,
            "net_alloc_mib": -1024 / 1048576.0,
        },
    }

    with tempfile.TemporaryDirectory() as tmpdir:
      output_path = os.path.join(tmpdir, "category_summary.pprof.pb.gz")
      analyzer.write_classification_summary_pprof(output_path, summary)
      result = subprocess.run(
          ["go", "tool", "pprof", "-raw", output_path],
          text=True,
          capture_output=True,
          check=False)

    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("Native heap summary", result.stdout)
    self.assertIn("il2cpp", result.stdout)
    self.assertIn("meta", result.stdout)
    self.assertIn("category:[il2cpp/meta]", result.stdout)

  def test_write_classification_pprof_files_removes_stale_category_files(self):
    """重跑分类输出前应删除旧分类文件，避免旧 hybridclr 结果污染新 UI 分类。"""
    callsites = {
        1: analyzer.Callsite(parent_id=None, frame_id=10, depth=0),
        2: analyzer.Callsite(parent_id=1, frame_id=20, depth=1),
        3: analyzer.Callsite(parent_id=1, frame_id=30, depth=1),
    }
    frame_labels = {
        10: "Root",
        20: "UIManager",
        30: "hybridclr::metadata",
    }
    ui_alloc = analyzer.Allocation(7, "libc.malloc", 2, 1, 4096)
    hybrid_alloc = analyzer.Allocation(7, "libc.malloc", 3, 1, 8192)
    classified = [
        (
            analyzer.ClassificationRule("fsui", ("UIManager",)),
            [analyzer.ClassifiedAllocation(ui_alloc, ("UIManager", "Root"))],
        ),
        (
            analyzer.ClassificationRule("hybridclr/other", ("hybridclr",)),
            [analyzer.ClassifiedAllocation(
                hybrid_alloc, ("hybridclr::metadata", "Root"))],
        ),
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
      stale_path = os.path.join(tmpdir, "01_hybridclr.pprof.pb.gz")
      with open(stale_path, "wb") as output:
        output.write(b"old hybridclr result with UIManager")

      analyzer.write_classification_pprof_files(
          tmpdir,
          classified,
          remaining=[],
          callsites=callsites,
          frame_labels=frame_labels,
          processes={7: (1234, "game")})

      self.assertFalse(os.path.exists(stale_path))
      self.assertTrue(os.path.exists(os.path.join(tmpdir, "01_fsui.pprof.pb.gz")))
      self.assertTrue(os.path.exists(os.path.join(tmpdir, "02_hybridclr.pprof.pb.gz")))

  def test_write_pprof_outputs_profile_readable_by_go_pprof(self):
    """pprof 输出必须能被 go tool pprof 读取，并保留正负净值口径。"""
    callsites = {
        1: analyzer.Callsite(parent_id=None, frame_id=10, depth=0),
        2: analyzer.Callsite(parent_id=1, frame_id=20, depth=1),
        3: analyzer.Callsite(parent_id=1, frame_id=30, depth=1),
    }
    frame_labels = {
        10: "Root",
        20: "Allocator",
        30: "Releaser",
    }
    allocations = [
        analyzer.Allocation(
            upid=7,
            heap_name="libc.malloc",
            callsite_id=2,
            net_alloc_count=3,
            net_alloc_bytes=4096),
        analyzer.Allocation(
            upid=7,
            heap_name="libc.malloc",
            callsite_id=3,
            net_alloc_count=-1,
            net_alloc_bytes=-1024),
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
      output_path = os.path.join(tmpdir, "native_heap.pprof.pb.gz")
      analyzer.write_pprof(
          output_path,
          "Native heap test",
          allocations,
          callsites,
          frame_labels,
          processes={7: (1234, "game")})

      result = subprocess.run(
          ["go", "tool", "pprof", "-raw", output_path],
          text=True,
          capture_output=True,
          check=False)

    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertRegex(result.stdout, r"Mappings\n1: .*native_heap")
    self.assertIn("positive_net_alloc_bytes", result.stdout)
    self.assertIn("absolute_net_alloc_bytes", result.stdout)
    self.assertIn("net_alloc_bytes", result.stdout)
    self.assertIn("Allocator", result.stdout)
    self.assertIn("Releaser", result.stdout)


if __name__ == "__main__":
  unittest.main()
