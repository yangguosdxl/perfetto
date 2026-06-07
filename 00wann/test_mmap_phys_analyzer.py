#!/usr/bin/env python3
"""mmap_phys_analyzer.py 的单元测试。"""

import json
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

import collect_mmap_phys_data as collector
import mmap_phys_analyzer as analyzer


class MmapPhysAnalyzerTest(unittest.TestCase):

  def test_default_trace_processor_uses_perfetto_root_from_config(self):
    """默认 trace_processor 应从 config.sh 的 PerfettoRoot 推导。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      config_path = os.path.join(tmpdir, "config.sh")
      with open(config_path, "w", encoding="utf-8") as f:
        f.write("export PerfettoRoot='perfetto-root'\n")

      expected = os.path.abspath(
          os.path.join(tmpdir, "perfetto-root/out/linux_clang_release/trace_processor_shell"))
      os.makedirs(os.path.dirname(expected))
      with open(expected, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\n")
      os.chmod(expected, 0o755)

      self.assertEqual(analyzer.find_default_tp(tmpdir), expected)

  def test_trace_processor_stderr_does_not_pollute_csv(self):
    """trace_processor 的日志不能拼进 CSV 字段。"""
    def fake_run(_cmd, **kwargs):
      stdout = '"ts","utid"\n"214992732002882","7"'
      stderr = "[482.196] query.cc:159 Query execution time: 13 ms\n"
      if kwargs.get("stderr") == analyzer.subprocess.STDOUT:
        stdout += stderr
        stderr = None
      return subprocess_result(returncode=0, stdout=stdout, stderr=stderr)

    with mock.patch.object(analyzer.subprocess, "run", side_effect=fake_run):
      rows = analyzer.run_tp_query("fake-tp", "fake-trace", "select 1")

    self.assertEqual(rows, [{"ts": "214992732002882", "utid": "7"}])

  def test_collector_csv_parser_strips_appended_trace_processor_log(self):
    """trace_processor 日志贴到 CSV 行尾时，也不能污染字段值。"""
    output = (
        '"event_id","ts","utid","event_name","arg_id","key","int_value",'
        '"string_value","value_type"\n'
        '"1","1000","7","sys_enter_mmap","10","len","4096","","int"'
        '[110.010]            query.cc:159 Query execution time: 45161 ms\n')

    rows = collector.parse_trace_processor_csv(
        output,
        required_columns=("event_id", "ts", "utid", "event_name", "arg_id",
                          "key", "int_value", "string_value", "value_type"))

    self.assertEqual(rows[0]["event_id"], "1")
    self.assertEqual(rows[0]["value_type"], "int")

  def test_repeated_raw_syscall_args_are_expanded_by_order(self):
    """raw_syscalls 的 repeated args 需要按行顺序展开成 arg0/arg1。"""
    rows = [
        {
            "event_id": "1",
            "ts": "1000",
            "utid": "7",
            "event_name": "sys_enter",
            "arg_id": "10",
            "key": "id",
            "int_value": str(analyzer.ARM64_MMAP_NR),
            "string_value": "",
            "value_type": "int",
        },
        {
            "event_id": "1",
            "ts": "1000",
            "utid": "7",
            "event_name": "sys_enter",
            "arg_id": "11",
            "key": "args",
            "int_value": str(0x10000000),
            "string_value": "",
            "value_type": "uint",
        },
        {
            "event_id": "1",
            "ts": "1000",
            "utid": "7",
            "event_name": "sys_enter",
            "arg_id": "12",
            "key": "args",
            "int_value": str(0x3000),
            "string_value": "",
            "value_type": "uint",
        },
    ]

    with mock.patch.object(analyzer, "run_tp_query", return_value=rows):
      events = analyzer.load_syscalls("fake-tp", "fake-trace")

    self.assertEqual(events[0].args["arg0"], 0x10000000)
    self.assertEqual(events[0].args["arg1"], 0x3000)

  def test_load_syscalls_filters_target_process_before_args(self):
    """主分析查询 syscall 时应先按目标 pid 缩小 ftrace，再扫描 args。"""
    seen_sql = []

    def fake_tp_query(_tp, _trace, sql):
      seen_sql.append(sql)
      return []

    with mock.patch.object(analyzer, "run_tp_query", side_effect=fake_tp_query):
      self.assertEqual(analyzer.load_syscalls("fake-tp", "fake-trace", pid=1234), [])

    self.assertEqual(len(seen_sql), 1)
    sql = seen_sql[0]
    self.assertIn("target_ftrace_events AS", sql)
    self.assertIn("LEFT JOIN __intrinsic_process pr ON th.upid = pr.id", sql)
    self.assertIn("WHERE (pr.pid = 1234 OR th.tid = 1234)", sql)
    self.assertIn("FROM target_ftrace_events tfe", sql)
    self.assertIn("JOIN __intrinsic_args a ON tfe.arg_set_id = a.arg_set_id", sql)
    self.assertNotIn("JOIN __intrinsic_args a ON fe.arg_set_id = a.arg_set_id", sql)

  def test_load_stacks_prefers_stack_profile_symbol_names(self):
    """调用栈展示应把 symbol_set_id 的 inline 符号拆成清晰 frame。"""
    def fake_tp_query(_tp, _trace, sql):
      if "stack_profile_symbol" in sql:
        return [
            {"symbol_set_id": "77", "id": "1", "name": "InlineAllocator"},
            {"symbol_set_id": "77", "id": "2", "name": "RealMmapCaller"},
        ]
      if "__intrinsic_stack_profile_callsite" in sql:
        return [
            {
                "id": "10",
                "parent_id": "-1",
                "frame_name": "0xabc",
                "deobfuscated_name": "",
                "mapping_name": "/data/app/libgame.so",
                "rel_pc": "16",
                "symbol_set_id": "77",
            }
        ]
      return []

    with mock.patch.object(analyzer, "run_tp_query", side_effect=fake_tp_query):
      stacks = analyzer.load_stacks("fake-tp", "fake-trace")

    self.assertEqual(
        stacks[10].frames,
        ["InlineAllocator [libgame.so]", "RealMmapCaller [libgame.so]"])

  def test_auto_smaps_timestamp_treats_collector_uptime_filename_as_ns(self):
    """采集脚本写入的 uptime 纳秒文件名不能被 auto 误判为毫秒。"""
    timestamp_ns = 50596950000000

    parsed = analyzer.parse_timestamp_from_name(
        f"/tmp/smaps/{timestamp_ns}.smaps",
        unit="auto")

    self.assertEqual(parsed, timestamp_ns)

  def test_symbolize_trace_concats_traceconv_symbols_like_heap_profile(self):
    """mmap 主分析应像 heap_profile.py 一样拼接 traceconv 符号包。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      raw_trace = os.path.join(tmpdir, "mmap_trace.perfetto-trace")
      with open(raw_trace, "wb") as fd:
        fd.write(b"raw-trace")

      calls = []

      def fake_call(cmd, **kwargs):
        calls.append((cmd, kwargs))
        kwargs["stdout"].write(b"symbol-packets")
        return 0

      with mock.patch.dict(os.environ, {"PERFETTO_BINARY_PATH": "/symbols"}), \
          mock.patch.object(collector.subprocess, "call", side_effect=fake_call):
        symbolized = collector.symbolize_trace("traceconv", raw_trace, tmpdir)

      self.assertEqual(symbolized, os.path.join(tmpdir, "symbolized-trace"))
      with open(symbolized, "rb") as fd:
        self.assertEqual(fd.read(), b"raw-tracesymbol-packets")
      self.assertEqual(calls[0][0], ["traceconv", "symbolize", raw_trace])
      self.assertEqual(calls[0][1]["env"]["PERFETTO_BINARY_PATH"], "/symbols")

  def test_perfetto_config_collects_raw_syscall_enter_and_exit(self):
    """采集 mmap 归因必须同时拿到 syscall 参数和返回值。"""
    config = collector.build_perfetto_config(
        name="com.example.app",
        duration_ms=1000,
        buffer_kb=1024,
        include_ftrace=True,
        kernel_frames=True,
        perf_ring_buffer_pages=4096,
        perf_ring_buffer_read_period_ms=100,
        include_mmap_callstacks=True)

    self.assertIn('ftrace_events: "raw_syscalls/sys_enter"', config)
    self.assertIn('ftrace_events: "raw_syscalls/sys_exit"', config)
    self.assertIn('tracepoint {\n          name: "raw_syscalls:sys_enter"', config)
    self.assertIn('name: "linux.process_stats"', config)
    self.assertIn("ring_buffer_pages: 4096", config)
    self.assertIn("ring_buffer_read_period_ms: 100", config)

  def test_start_perfetto_streams_config_via_stdin(self):
    """启动 Perfetto 时通过 stdin 传配置，避免设备路径读取权限影响。"""
    calls = []

    def fake_check_output(cmd, **kwargs):
      calls.append((cmd, kwargs))
      return b"12345\n"

    with mock.patch.object(collector.subprocess, "check_output",
                           side_effect=fake_check_output):
      pid = collector.start_perfetto(
          "buffers {}\n",
          "/data/misc/perfetto-traces/test-trace",
          no_guardrails=False)

    self.assertEqual(pid, 12345)
    self.assertEqual(calls[0][0], [
        "adb", "shell", "perfetto", "--txt", "-c", "-",
        "-o", "/data/misc/perfetto-traces/test-trace", "-d"
    ])
    self.assertEqual(calls[0][1]["input"], b"buffers {}\n")
    self.assertEqual(calls[0][1]["stderr"], collector.subprocess.STDOUT)

  def test_start_perfetto_reports_device_error_output(self):
    """Perfetto 启动失败时应输出设备侧错误，便于直接定位原因。"""
    def fake_check_output(cmd, **_kwargs):
      raise collector.subprocess.CalledProcessError(
          1,
          cmd,
          output=b"Could not open config (errno: 13, Permission denied)\n")

    with mock.patch.object(collector.subprocess, "check_output",
                           side_effect=fake_check_output):
      with self.assertRaisesRegex(RuntimeError, "Permission denied"):
        collector.start_perfetto(
            "buffers {}\n",
            "/data/misc/perfetto-traces/test-trace",
            no_guardrails=False)

  def test_default_config_collects_mmap_callstacks_without_heapprofd(self):
    """默认主功能只采 mmap 调用栈，不再启用 heapprofd。"""
    config = collector.build_perfetto_config(
        name="com.example.app",
        duration_ms=1000,
        buffer_kb=1024,
        include_ftrace=True,
        kernel_frames=True)

    self.assertIn('ftrace_events: "raw_syscalls/sys_enter"', config)
    self.assertNotIn('name: "android.heapprofd"', config)
    self.assertIn('name: "linux.perf"', config)
    self.assertIn('name: "linux.process_stats"', config)
    self.assertIn("callstack_sampling", config)

  def test_validation_config_does_not_collect_mmap_callstacks(self):
    """显式验证模式不采 mmap 调用栈，也不启用 heapprofd。"""
    config = collector.build_perfetto_config(
        name="com.example.app",
        duration_ms=1000,
        buffer_kb=1024,
        include_ftrace=True,
        kernel_frames=True,
        include_mmap_callstacks=False)

    self.assertIn('syscall_events: "sys_mmap"', config)
    self.assertIn('syscall_events: "sys_munmap"', config)
    self.assertIn('syscall_events: "sys_mremap"', config)
    self.assertIn('ftrace_events: "raw_syscalls/sys_enter"', config)
    self.assertIn('ftrace_events: "raw_syscalls/sys_exit"', config)
    self.assertIn('ftrace_events: "sched/sched_switch"', config)
    self.assertIn('name: "linux.process_stats"', config)
    self.assertNotIn('name: "android.heapprofd"', config)
    self.assertNotIn('name: "linux.perf"', config)
    self.assertNotIn("callstack_sampling", config)

  def test_collect_defaults_to_75_seconds(self):
    """默认 mmap 物理内存采集时长应为 1 分 15 秒。"""
    with mock.patch.object(sys, "argv", ["collect_mmap_phys_data.py", "--name", "app"]):
      args = collector.parse_args()

    self.assertEqual(args.duration_ms, 75000)
    self.assertTrue(args.mmap_callstacks)

  def test_collect_accepts_analyzer_classification_args(self):
    """采集入口应接受离线分析分类参数，供包装脚本设置默认值。"""
    with mock.patch.object(sys, "argv", [
        "collect_mmap_phys_data.py", "--name", "app", "--classify-config",
        "heap_analyzer/fs.ini", "--top-n", "0"
    ]):
      args = collector.parse_args()

    self.assertEqual(args.classify_config, "heap_analyzer/fs.ini")
    self.assertEqual(args.top_n, 0)

  def test_run_analyzer_forwards_classify_config_and_top_n(self):
    """采集完成后应把分类配置和 top_n 转交给 mmap 离线分析器。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      analyzer_path = os.path.join(tmpdir, "mmap_phys_analyzer.py")
      with open(analyzer_path, "w", encoding="utf-8") as fd:
        fd.write("#!/usr/bin/env python3\n")
      args = collector.argparse.Namespace(
          output=tmpdir,
          trace_processor="tp",
          analyzer=analyzer_path,
          classify_config="heap_analyzer/fs.ini",
          top_n=0)
      with mock.patch.object(collector.subprocess, "check_call") as check_call:
        collector.run_analyzer(args, 1234, "symbolized-trace", "smaps")

    cmd = check_call.call_args.args[0]
    self.assertIn("--classify-config", cmd)
    self.assertIn("heap_analyzer/fs.ini", cmd)
    self.assertIn("--top-n", cmd)
    self.assertIn("0", cmd)

  def test_collect_cli_no_longer_exposes_malloc_collection(self):
    """mmap 采集脚本不再暴露 malloc/heapprofd 采集参数。"""
    with mock.patch.object(sys, "argv", ["collect_mmap_phys_data.py", "--name", "app"]):
      args = collector.parse_args()

    self.assertFalse(hasattr(args, "collect_malloc"))
    self.assertFalse(hasattr(args, "malloc_sampling_interval_bytes"))
    self.assertFalse(hasattr(args, "malloc_shmem_size_bytes"))

    with mock.patch.object(sys, "argv", [
        "collect_mmap_phys_data.py", "--name", "app", "--malloc"
    ]):
      with self.assertRaises(SystemExit):
        collector.parse_args()

  def test_main_captures_meminfo_before_trace_analysis(self):
    """采样结束后应先保存 meminfo，再运行 trace 健康检查和离线分析。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      args = collector.argparse.Namespace(
          name="com.example.app",
          duration_ms=75000,
          smaps_interval_ms=1000,
          output=tmpdir,
          wait_timeout_s=120,
          buffer_kb=262144,
          perf_ring_buffer_pages=8192,
          perf_ring_buffer_read_period_ms=100,
          mmap_callstacks=True,
          no_ftrace=False,
          no_kernel_frames=False,
          no_guardrails=False,
          use_su=False,
          no_analyze=False,
          trace_processor="tp",
          traceconv="traceconv",
          analyzer="analyzer.py")
      calls = []

      def record(name, result=None):
        def inner(*_args, **_kwargs):
          calls.append(name)
          return result
        return inner

      with mock.patch.object(collector, "parse_args", return_value=args), \
          mock.patch.object(collector, "wait_for_pid", return_value=1234), \
          mock.patch.object(collector, "write_config", record("write_config")), \
          mock.patch.object(collector, "start_perfetto",
                            record("start_perfetto", 5678)), \
          mock.patch.object(collector, "collect_smaps", record("collect_smaps")), \
          mock.patch.object(collector, "pull_trace", record("pull_trace")), \
          mock.patch.object(collector, "capture_meminfo",
                            record("capture_meminfo", os.path.join(tmpdir, "dumpsys_meminfo.txt"))), \
          mock.patch.object(collector, "symbolize_trace",
                            record("symbolize_trace", os.path.join(tmpdir, "symbolized-trace"))), \
          mock.patch.object(collector, "check_trace_health",
                            record("check_trace_health", {})), \
          mock.patch.object(collector, "run_analyzer", record("run_analyzer")), \
          mock.patch.object(collector, "collect_memory_validation",
                            record("collect_memory_validation")):
        self.assertEqual(collector.main(), 0)

    self.assertIn("capture_meminfo", calls)
    self.assertLess(calls.index("capture_meminfo"), calls.index("symbolize_trace"))
    self.assertLess(calls.index("capture_meminfo"), calls.index("check_trace_health"))
    self.assertLess(calls.index("capture_meminfo"), calls.index("run_analyzer"))
    self.assertLess(calls.index("capture_meminfo"),
                    calls.index("collect_memory_validation"))

  def test_run_collection_analyzes_symbolized_trace_when_callstacks_enabled(self):
    """主功能生成 symbolized-trace 后，离线分析必须读取符号化 trace。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      args = collector.argparse.Namespace(
          name="com.example.app",
          duration_ms=75000,
          smaps_interval_ms=1000,
          output=tmpdir,
          wait_timeout_s=120,
          buffer_kb=262144,
          perf_ring_buffer_pages=8192,
          perf_ring_buffer_read_period_ms=100,
          mmap_callstacks=True,
          no_ftrace=False,
          no_kernel_frames=False,
          no_guardrails=False,
          use_su=False,
          no_analyze=False,
          trace_processor="tp",
          traceconv="traceconv",
          analyzer="analyzer.py")
      analyzed_traces = []

      def fake_run_analyzer(_args, _pid, trace_path, _smaps_dir):
        analyzed_traces.append(trace_path)

      with mock.patch.object(collector, "wait_for_pid", return_value=1234), \
          mock.patch.object(collector, "write_config"), \
          mock.patch.object(collector, "start_perfetto", return_value=5678), \
          mock.patch.object(collector, "collect_smaps"), \
          mock.patch.object(collector, "pull_trace"), \
          mock.patch.object(collector, "symbolize_trace",
                            return_value=os.path.join(tmpdir, "symbolized-trace")), \
          mock.patch.object(collector, "capture_meminfo",
                            return_value=os.path.join(tmpdir, "dumpsys_meminfo.txt")), \
          mock.patch.object(collector, "check_trace_health", return_value={}), \
          mock.patch.object(collector, "run_analyzer",
                            side_effect=fake_run_analyzer), \
          mock.patch.object(collector, "collect_memory_validation",
                            return_value={"trace_health": {}}):
        result = collector.run_collection(args)

    self.assertEqual(result["status"], 0)
    self.assertEqual(analyzed_traces, [os.path.join(tmpdir, "symbolized-trace")])

  def test_main_no_mmap_callstacks_restarts_app_before_validation(self):
    """无栈验证应先重启目标 App，并让 Perfetto 先于 App 启动。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      args = collector.argparse.Namespace(
          name="com.example.app",
          duration_ms=120000,
          smaps_interval_ms=1000,
          output=tmpdir,
          wait_timeout_s=120,
          buffer_kb=262144,
          perf_ring_buffer_pages=8192,
          perf_ring_buffer_read_period_ms=100,
          mmap_callstacks=False,
          no_ftrace=False,
          no_kernel_frames=False,
          no_guardrails=False,
          use_su=False,
          no_analyze=False,
          trace_processor="tp",
          analyzer="analyzer.py")
      calls = []

      def fake_run_collection(run_args, start_target_after_perfetto=False):
        calls.append(("run_collection", start_target_after_perfetto))
        return {"status": 0}

      with mock.patch.object(collector, "parse_args", return_value=args), \
          mock.patch.object(collector, "force_stop_app",
                            side_effect=lambda name: calls.append(("force_stop", name))), \
          mock.patch.object(collector, "run_collection",
                            side_effect=fake_run_collection):
        self.assertEqual(collector.main(), 0)

    self.assertEqual(calls, [
        ("force_stop", "com.example.app"),
        ("run_collection", True),
    ])

  def test_collection_can_start_perfetto_before_launching_app(self):
    """验证 attempt 应先启动 Perfetto，再拉起 App，避免错过启动期分配。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      args = collector.argparse.Namespace(
          name="com.example.app",
          duration_ms=75000,
          smaps_interval_ms=1000,
          output=tmpdir,
          wait_timeout_s=120,
          buffer_kb=262144,
          perf_ring_buffer_pages=8192,
          perf_ring_buffer_read_period_ms=100,
          mmap_callstacks=False,
          no_ftrace=False,
          no_kernel_frames=False,
          no_guardrails=False,
          use_su=False,
          no_analyze=False,
          trace_processor="tp",
          analyzer="analyzer.py")
      calls = []

      def record(name, result=None):
        def inner(*_args, **_kwargs):
          calls.append(name)
          return result
        return inner

      with mock.patch.object(collector, "write_config", record("write_config")), \
          mock.patch.object(collector, "start_perfetto",
                            record("start_perfetto", 5678)), \
          mock.patch.object(collector, "wait_for_pid",
                            record("wait_for_pid", 1234)), \
          mock.patch.object(collector, "collect_smaps", record("collect_smaps")), \
          mock.patch.object(collector, "pull_trace", record("pull_trace")), \
          mock.patch.object(collector, "capture_meminfo",
                            record("capture_meminfo",
                                   os.path.join(tmpdir, "dumpsys_meminfo.txt"))), \
          mock.patch.object(collector, "collect_memory_validation",
                            record("collect_memory_validation",
                                   {"trace_health": {}})):
        result = collector.run_collection(args, start_target_after_perfetto=True)

    self.assertEqual(result["status"], 0)
    self.assertLess(calls.index("start_perfetto"), calls.index("wait_for_pid"))
    self.assertLess(calls.index("wait_for_pid"), calls.index("collect_smaps"))

  def test_no_mmap_callstacks_does_not_enable_heapprofd(self):
    """无栈验证不能生成 heapprofd 配置。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      args = collector.argparse.Namespace(
          name="com.example.app",
          duration_ms=75000,
          smaps_interval_ms=1000,
          output=tmpdir,
          wait_timeout_s=120,
          buffer_kb=262144,
          perf_ring_buffer_pages=8192,
          perf_ring_buffer_read_period_ms=100,
          mmap_callstacks=False,
          no_ftrace=False,
          no_kernel_frames=False,
          no_guardrails=False,
          use_su=False,
          no_analyze=True,
          trace_processor=None,
          analyzer="analyzer.py")
      configs = []

      def fake_write_config(config, _output_dir):
        configs.append(config)

      with mock.patch.object(collector, "write_config", side_effect=fake_write_config), \
          mock.patch.object(collector, "wait_for_pid", return_value=1234), \
          mock.patch.object(collector, "start_perfetto", return_value=5678), \
          mock.patch.object(collector, "collect_smaps"), \
          mock.patch.object(collector, "pull_trace"), \
          mock.patch.object(collector, "capture_meminfo",
                            return_value=os.path.join(tmpdir, "dumpsys_meminfo.txt")), \
          mock.patch.object(collector, "collect_memory_validation",
                            return_value={"trace_health": {}}):
        result = collector.run_collection(args)

    self.assertEqual(result["status"], 0)
    self.assertNotIn('name: "android.heapprofd"', configs[0])

  def test_trace_health_summary_flags_perf_lost_records(self):
    """健康检查需要区分 Perfetto buffer 和 perf 内核 buffer 丢数。"""
    rows = [
        {"name": "traced_buf_buffer_size", "idx": "0", "value": str(256 * 1024 * 1024)},
        {"name": "traced_buf_bytes_written", "idx": "0", "value": str(32 * 1024 * 1024)},
        {"name": "traced_buf_bytes_overwritten", "idx": "0", "value": "0"},
        {"name": "traced_buf_chunks_overwritten", "idx": "0", "value": "0"},
        {"name": "traced_buf_trace_writer_packet_loss", "idx": "0", "value": "0"},
        {"name": "ftrace_cpu_overrun_delta", "idx": "0", "value": "0"},
        {"name": "ftrace_cpu_dropped_events_delta", "idx": "0", "value": "0"},
        {"name": "perf_cpu_lost_records", "idx": "0", "value": "12"},
        {"name": "perf_cpu_lost_records", "idx": "1", "value": "8"},
    ]

    summary = collector.summarize_trace_health(rows)

    self.assertEqual(summary["perfetto_data_loss"], 0)
    self.assertEqual(summary["ftrace_data_loss"], 0)
    self.assertEqual(summary["perf_data_loss"], 20)
    self.assertEqual(summary["buffer_size_bytes"], 256 * 1024 * 1024)
    self.assertEqual(summary["bytes_written"], 32 * 1024 * 1024)

  def test_trace_health_summary_ignores_heapprofd_rows(self):
    """mmap 采集健康检查不再把 heapprofd 统计纳入结果。"""
    rows = [
        {"name": "heapprofd_buffer_overran", "idx": "7045", "value": "1"},
        {"name": "heapprofd_client_error", "idx": "7045", "value": "1"},
        {"name": "ftrace_cpu_read_events_delta", "idx": "0", "value": "20"},
        {"name": "ftrace_cpu_read_events_delta", "idx": "1", "value": "30"},
    ]

    summary = collector.summarize_trace_health(rows)

    self.assertEqual(summary["ftrace_read_events"], 50)
    self.assertNotIn("heapprofd_data_loss", summary)
    self.assertNotIn("heapprofd_errors", summary)

  def test_trace_health_print_does_not_mention_heapprofd(self):
    """终端健康检查只输出 mmap 采集相关 buffer 状态。"""
    out = io.StringIO()
    summary = {
        "buffer_size_bytes": 256 * 1024 * 1024,
        "bytes_written": 16 * 1024 * 1024,
        "perfetto_data_loss": 0,
        "ftrace_data_loss": 0,
        "perf_data_loss": 0,
    }

    with mock.patch.object(sys, "stdout", out):
      collector.print_trace_health(summary)

    self.assertNotIn("heapprofd", out.getvalue())

  def test_wait_status_includes_elapsed_seconds(self):
    """等待应用启动的进度需要在同一行展示累计秒数。"""
    status = collector.format_wait_status("com.example.app", 12.34)

    self.assertEqual(status, "等待应用启动: com.example.app 已等待 12.3s")

  def test_wait_for_pid_launches_app_once_when_not_running(self):
    """目标进程未启动时，应自动拉起游戏并继续等待 pid。"""
    pidof_outputs = ["", "4321"]
    shell_calls = []

    def fake_adb_shell(cmd, **_kwargs):
      shell_calls.append(cmd)
      if cmd.startswith("pidof "):
        return pidof_outputs.pop(0)
      return "monkey ok"

    with mock.patch.object(collector, "adb_shell", side_effect=fake_adb_shell), \
         mock.patch.object(collector.time, "sleep"):
      pid = collector.wait_for_pid("com.example.app", timeout_s=5)

    self.assertEqual(pid, 4321)
    self.assertEqual(
        shell_calls,
        [
            "pidof 'com.example.app' || true",
            "monkey -p 'com.example.app' 1",
            "pidof 'com.example.app' || true",
        ])

  def test_wait_for_pid_does_not_launch_when_already_running(self):
    """目标进程已存在时，不应额外发送 monkey 启动命令。"""
    shell_calls = []

    def fake_adb_shell(cmd, **_kwargs):
      shell_calls.append(cmd)
      return "4321"

    with mock.patch.object(collector, "adb_shell", side_effect=fake_adb_shell):
      pid = collector.wait_for_pid("com.example.app", timeout_s=5)

    self.assertEqual(pid, 4321)
    self.assertEqual(shell_calls, ["pidof 'com.example.app' || true"])

  def test_two_line_progress_reuses_existing_lines(self):
    """smaps 采样进度应复用两行输出，避免每次采样都刷屏。"""
    out = io.StringIO()
    progress = collector.TwoLineProgress(stream=out)

    progress.update("+ adb shell cat /proc/uptime", "smaps 快照: first.smaps (10 bytes)")
    progress.update("+ adb shell cat /proc/uptime", "smaps 快照: second.smaps (20 bytes)")

    self.assertEqual(
        out.getvalue(),
        "\r\033[K+ adb shell cat /proc/uptime\n"
        "\r\033[Ksmaps 快照: first.smaps (10 bytes)\n"
        "\033[2F"
        "\r\033[K+ adb shell cat /proc/uptime\n"
        "\r\033[Ksmaps 快照: second.smaps (20 bytes)\n")

  def test_smaps_progress_line_includes_remaining_time(self):
    """smaps 快照输出应同时展示本次采集的剩余时间。"""
    line = collector.format_smaps_progress_line("snapshot.smaps", 5345265, 12.34)

    self.assertEqual(line, "smaps 快照: snapshot.smaps (5345265 bytes) 剩余: 12.3s")

  def test_read_smaps_falls_back_to_su_when_plain_cat_is_invalid(self):
    """默认参数下普通 cat 无权限时，应自动尝试 su 0。"""
    calls = []

    def fake_check_output(cmd, **_kwargs):
      calls.append(cmd)
      if "su" in cmd:
        return b"1000-2000 rw-p 00000000 00:00 0 [anon:test]\nRss: 4 kB\nPss: 4 kB\n"
      return b"cat: /proc/1234/smaps: Permission denied\n"

    with mock.patch.object(collector.subprocess, "check_output",
                           side_effect=fake_check_output):
      data = collector.read_smaps(1234, use_su=False)

    self.assertIn(b"1000-2000", data)
    self.assertEqual(calls[0], ["adb", "exec-out", "cat", "/proc/1234/smaps"])
    self.assertEqual(calls[1], ["adb", "exec-out", "su", "0", "cat", "/proc/1234/smaps"])

  def test_read_smaps_falls_back_to_run_as_before_su(self):
    """无 root 调试包应先用 run-as 读取自身 smaps，再考虑 su。"""
    calls = []

    def fake_check_output(cmd, **_kwargs):
      calls.append(cmd)
      if "run-as" in cmd:
        return b"1000-2000 rw-p 00000000 00:00 0 [anon:test]\nRss: 4 kB\nPss: 4 kB\n"
      return b"cat: /proc/1234/smaps: Permission denied\n"

    with mock.patch.object(collector.subprocess, "check_output",
                           side_effect=fake_check_output):
      data = collector.read_smaps(
          1234, use_su=False, run_as_package="com.example.app")

    self.assertIn(b"1000-2000", data)
    self.assertEqual(calls[0], ["adb", "exec-out", "cat", "/proc/1234/smaps"])
    self.assertEqual(calls[1], [
        "adb", "exec-out", "run-as", "com.example.app",
        "cat", "/proc/1234/smaps"
    ])
    self.assertEqual(len(calls), 2)

  def test_parse_dumpsys_meminfo_summary(self):
    """meminfo 对比需要解析 Native Heap 与 TOTAL 的关键字段。"""
    text = """
Applications Memory Usage (in Kilobytes):
                   Pss  Private  Private  SwapPss     Heap     Heap     Heap
                 Total    Dirty    Clean    Dirty     Size    Alloc     Free
                ------   ------   ------   ------   ------   ------   ------
  Native Heap    12000    11000        0        0    64000    32000    32000
        TOTAL    50000    42000     1000        0   100000    50000    50000
"""

    summary = collector.parse_meminfo_summary(text)

    self.assertEqual(summary["native_heap_pss_bytes"], 12000 * 1024)
    self.assertEqual(summary["native_heap_alloc_bytes"], 32000 * 1024)
    self.assertEqual(summary["total_pss_bytes"], 50000 * 1024)

  def test_parse_dumpsys_meminfo_total_pss_line_with_commas(self):
    """TOTAL PSS 行和带逗号数字不能被其他 TOTAL 行覆盖。"""
    text = """
  Native Heap    12,000    11,000        0        0    64,000    32,000    32,000
TOTAL PSS: 50,000K
TOTAL SWAP PSS: 7,000K
"""

    summary = collector.parse_meminfo_summary(text)

    self.assertEqual(summary["native_heap_pss_bytes"], 12000 * 1024)
    self.assertEqual(summary["native_heap_alloc_bytes"], 32000 * 1024)
    self.assertEqual(summary["total_pss_bytes"], 50000 * 1024)

  def test_query_mmap_validation_syscalls_does_not_read_callstacks(self):
    """mmap 验证只查目标进程 raw syscall，不读取 mmap 调用栈。"""
    seen_sql = []

    def fake_run(cmd, **_kwargs):
      seen_sql.append(cmd[-1])
      return subprocess_result(
          returncode=0,
          stdout='"event_id","ts","utid","event_name","arg_id","key","int_value","string_value","value_type"\n',
          stderr=None)

    with mock.patch.object(collector.subprocess, "run", side_effect=fake_run):
      syscalls = collector.query_mmap_validation_syscalls(
          "fake-tp", "fake-trace", 1234)

    self.assertEqual(syscalls, [])
    self.assertFalse(any("stack_profile" in sql or "callsite" in sql or
                         "__intrinsic_perf_sample" in sql
                         for sql in seen_sql))

  def test_query_memory_validation_inputs_loads_trace_once_without_malloc(self):
    """无栈健康检查应一次查询拿齐 mmap 输入，但不读取 malloc 表。"""
    calls = []

    def fake_run(cmd, **_kwargs):
      calls.append(cmd)
      return subprocess_result(
          returncode=0,
          stdout=(
              '"section","c0","c1","c2","c3","c4","c5","c6","c7","c8","c9"\n'
              '"health","traced_buf_buffer_size","0","1024","","","","","","",""\n'
              f'"syscall","1","1000","7","raw_syscalls/sys_enter","10","id",'
              f'"{collector.ARM64_MMAP_NR}","","int",""\n'
              '"syscall","1","1000","7","raw_syscalls/sys_enter","11","args",'
              '"268435456","","uint",""\n'
              '"syscall"\n'
              '"syscall","1","1000","7","raw_syscalls/sys_enter","12","args",'
              '"4096","","uint",""\n'
              '"syscall","2","1001","7","raw_syscalls/sys_exit","13","ret",'
              '"536870912","","int",""\n'),
          stderr=None)

    with mock.patch.object(collector.subprocess, "run", side_effect=fake_run):
      inputs = collector.query_memory_validation_inputs(
          "fake-tp", "fake-trace", 1234)

    self.assertEqual(len(calls), 1)
    self.assertEqual(inputs["trace_health"]["buffer_size_bytes"], 1024)
    self.assertNotIn("malloc_summary", inputs)
    self.assertEqual(len(inputs["syscalls"]), 2)
    self.assertEqual(inputs["syscalls"][0].args["arg1"], 4096)
    self.assertNotIn("heap_profile_allocation", calls[0][-1])
    self.assertNotIn("latest_dump", calls[0][-1])

  def test_memory_validation_syscall_sql_filters_target_events_before_args(self):
    """raw syscall 量大时，SQL 应先下推目标 pid 和事件名，再扫描 args。"""
    sql = collector.build_memory_validation_inputs_sql(1234)

    self.assertIn("target_ftrace_events AS", sql)
    self.assertIn("LEFT JOIN __intrinsic_process pr ON th.upid = pr.id", sql)
    self.assertIn("WHERE (pr.pid = 1234 OR th.tid = 1234)", sql)
    self.assertIn("raw_syscall_events AS", sql)
    self.assertIn("FROM target_ftrace_events tfe", sql)
    self.assertIn("JOIN __intrinsic_args a ON tfe.arg_set_id = a.arg_set_id", sql)
    self.assertNotIn("JOIN __intrinsic_args a ON fe.arg_set_id = a.arg_set_id", sql)

  def test_write_memory_validation_report_omits_malloc_when_disabled(self):
    """无栈验证报告不输出 malloc 与 Native Heap Alloc 对比。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      meminfo_path = os.path.join(tmpdir, "dumpsys_meminfo.txt")
      with open(meminfo_path, "w", encoding="utf-8") as fd:
        fd.write("Native Heap 10 0 0 0 20 4 16\nTOTAL 12 0 0 0 0 0 0\n")

      report_path = collector.write_memory_validation_report(
          output_dir=tmpdir,
          mmap_summary={
              "pss_bytes": 3072,
              "rss_bytes": 4096,
              "virtual_bytes": 8192,
              "syscall_events": 2,
              "smaps_snapshots": 1,
          },
          meminfo_path=meminfo_path)

      with open(report_path, "r", encoding="utf-8") as fd:
        report = json.load(fd)

    self.assertNotIn("malloc", report)
    self.assertEqual(report["mmap"]["pss_bytes"], 3072)
    self.assertNotIn("tracked_sum", report)
    self.assertNotIn("tracked_sum_minus_meminfo_total_pss_bytes",
                     report["comparison"])
    self.assertNotIn("malloc_live_minus_meminfo_native_heap_alloc_bytes",
                     report["comparison"])
    self.assertEqual(report["meminfo"]["native_heap_alloc_bytes"], 4 * 1024)
    self.assertEqual(report["validation"]["status"], "pass")

  def test_memory_validation_report_fails_when_mmap_events_are_missing(self):
    """有 smaps 但没有 mmap syscall 时，验证报告必须显式失败。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      meminfo_path = os.path.join(tmpdir, "dumpsys_meminfo.txt")
      with open(meminfo_path, "w", encoding="utf-8") as fd:
        fd.write("Native Heap 10 0 0 0 20 4 16\nTOTAL 12 0 0 0 0 0 0\n")

      report_path = collector.write_memory_validation_report(
          output_dir=tmpdir,
          mmap_summary={"pss_bytes": 0, "syscall_events": 0, "smaps_snapshots": 3},
          meminfo_path=meminfo_path,
          trace_health={"ftrace_data_loss": 1})

      with open(report_path, "r", encoding="utf-8") as fd:
        report = json.load(fd)

    self.assertEqual(report["validation"]["status"], "fail")
    self.assertIn("mmap_syscall_events_missing", report["validation"]["issues"])
    self.assertIn("ftrace_data_loss", report["validation"]["issues"])

  def test_mmap_perf_sample_and_munmap_are_attributed_to_smaps_pss(self):
    """构造 mmap/perf/smaps 样例，验证释放区间不会继续计入物理占用。"""
    with tempfile.TemporaryDirectory() as tmpdir:
      smaps_dir = os.path.join(tmpdir, "smaps")
      os.mkdir(smaps_dir)
      smaps_path = os.path.join(smaps_dir, "2000.smaps")
      with open(smaps_path, "w", encoding="utf-8") as fd:
        fd.write(
            "10001000-10004000 rw-p 00000000 00:00 0 [anon:mmap-test]\n"
            "Size:                 12 kB\n"
            "Rss:                  12 kB\n"
            "Pss:                  12 kB\n"
            "Private_Clean:         0 kB\n"
            "Private_Dirty:        12 kB\n"
            "Shared_Clean:          0 kB\n"
            "Shared_Dirty:          0 kB\n")

      with mock.patch.object(analyzer, "run_tp_query", side_effect=self._fake_tp_query):
        samples = analyzer.load_perf_samples("fake-tp", "fake-trace")
        stacks = analyzer.load_stacks("fake-tp", "fake-trace")
        syscalls = analyzer.load_syscalls("fake-tp", "fake-trace")

      lifecycle = analyzer.build_lifecycle_events(
          syscalls, samples, stack_window_ns=10_000_000)
      snapshots = analyzer.load_snapshots(
          smaps_dir, pid=1234, unit="ns", offset_ns=0)
      output, summary_items, all_summary_items = analyzer.build_chrome_trace(
          snapshots, lifecycle, stacks, top_n=10)
      self.assertEqual(summary_items, all_summary_items)
      output_path = os.path.join(tmpdir, "mmap_phys_attribution_test.json")
      with open(output_path, "w", encoding="utf-8") as fd:
        json.dump(output, fd, ensure_ascii=False)

      speedscope = analyzer.build_speedscope(summary_items)
      speedscope_path = os.path.join(tmpdir, "mmap_phys_attribution_test.speedscope.json")
      with open(speedscope_path, "w", encoding="utf-8") as fd:
        json.dump(speedscope, fd, ensure_ascii=False)

      self.assertTrue(os.path.exists(output_path))
      self.assertTrue(os.path.exists(speedscope_path))
      keep_output_path = os.getenv("MMAP_PHYS_TEST_OUTPUT")
      if keep_output_path:
        with open(keep_output_path, "w", encoding="utf-8") as fd:
          json.dump(output, fd, ensure_ascii=False)
      keep_speedscope_path = os.getenv("MMAP_PHYS_TEST_SPEEDSCOPE_OUTPUT")
      if keep_speedscope_path:
        with open(keep_speedscope_path, "w", encoding="utf-8") as fd:
          json.dump(speedscope, fd, ensure_ascii=False)
      with open(output_path, "r", encoding="utf-8") as fd:
        output = json.load(fd)
      with open(speedscope_path, "r", encoding="utf-8") as fd:
        speedscope = json.load(fd)

      counters = [
          event for event in output["traceEvents"]
          if event.get("name") == "mmap stack PSS"
      ]
      self.assertEqual(len(counters), 1)
      args = counters[0]["args"]
      self.assertEqual(args["pss_bytes"], 12 * 1024)
      self.assertEqual(args["rss_bytes"], 12 * 1024)
      self.assertEqual(args["virtual_bytes"], 12 * 1024)
      self.assertEqual(args["private_dirty_bytes"], 12 * 1024)

      # Chrome JSON counter 事件的 args 必须保持数值，否则 Perfetto 会报
      # json_parser_failure；字符串详情放到同时间点的 instant 事件。
      for event in output["traceEvents"]:
        if event.get("ph") == "C":
          for value in event.get("args", {}).values():
            self.assertIsInstance(value, (int, float))

      details = [
          event for event in output["traceEvents"]
          if event.get("name") == "mmap stack details"
      ]
      self.assertEqual(len(details), 1)
      self.assertIn("AllocateByMmap", details[0]["args"]["stack"])
      self.assertIn("[anon:mmap-test]", details[0]["args"]["paths"])

      snapshot_details = [
          event for event in output["traceEvents"]
          if event.get("name") == "mmap snapshot details"
      ]
      self.assertEqual(len(snapshot_details), 1)
      self.assertEqual(snapshot_details[0]["args"]["smaps"], smaps_path)

      # 输出必须是 Perfetto UI 可加载的 Chrome JSON trace 基本结构。
      self.assertIn("traceEvents", output)
      self.assertEqual(output["traceEvents"][0]["name"], "process_name")
      summary = output["metadata"]["final_summary"]
      self.assertEqual(len(summary), 1)
      self.assertEqual(summary[0]["pss_bytes"], 12 * 1024)
      self.assertEqual(summary[0]["rss_bytes"], 12 * 1024)
      self.assertEqual(summary[0]["virtual_bytes"], 12 * 1024)
      self.assertEqual(summary[0]["stack"][0], "AllocateByMmap [libgame.so]")
      self.assertEqual(summary[0]["stack"][1], "GameInit [libgame.so]")

      self.assertEqual(speedscope["profiles"][0]["unit"], "bytes")
      self.assertEqual(speedscope["profiles"][0]["weights"], [12 * 1024])
      frame_names = [frame["name"] for frame in speedscope["shared"]["frames"]]
      self.assertIn("AllocateByMmap [libgame.so]", frame_names)
      self.assertIn("GameInit [libgame.so]", frame_names)

  def test_mmap_without_length_uses_smaps_vma_for_physical_attribution(self):
    """raw syscall 缺少 mmap length 时，用返回地址所在 VMA 做物理归因。"""
    snapshot = analyzer.Snapshot(
        ts=2000,
        pid=1234,
        path="fake.smaps",
        vmas=[
            analyzer.SmapsVma(
                start=0x10000000,
                end=0x10003000,
                pathname="[anon:raw-mmap]",
                rss_kb=12,
                pss_kb=12,
                private_dirty_kb=12)
        ])
    lifecycle = [
        (1500, "mmap", {
            "pid": 1234,
            "addr": 0x10000000,
            "size": 0,
            "stack_id": 10,
            "path": "",
        })
    ]
    stacks = {10: analyzer.Stack(10, ["AllocateByRawMmap"])}

    output, summary, all_summary = analyzer.build_chrome_trace(
        [snapshot], lifecycle, stacks, top_n=10)

    self.assertEqual(summary[0]["pss_bytes"], 12 * 1024)
    self.assertEqual(summary, all_summary)
    self.assertEqual(summary[0]["rss_bytes"], 12 * 1024)
    self.assertEqual(summary[0]["virtual_bytes"], 12 * 1024)
    self.assertIn("AllocateByRawMmap", output["metadata"]["final_summary"][0]["stack"])

  def test_multiple_unknown_length_mmaps_in_one_vma_do_not_duplicate_pss(self):
    """同一个 VMA 被多个未知长度 mmap 命中时，PSS 总和不能超过 smaps。"""
    snapshot = analyzer.Snapshot(
        ts=2000,
        pid=1234,
        path="fake.smaps",
        vmas=[
            analyzer.SmapsVma(
                start=0x10000000,
                end=0x10003000,
                pathname="[anon:shared-vma]",
                rss_kb=12,
                pss_kb=12,
                private_dirty_kb=12)
        ])
    lifecycle = [
        (1500, "mmap", {
            "pid": 1234,
            "addr": 0x10000000,
            "size": 0,
            "stack_id": 10,
            "path": "",
        }),
        (1501, "mmap", {
            "pid": 1234,
            "addr": 0x10001000,
            "size": 0,
            "stack_id": 11,
            "path": "",
        }),
    ]
    stacks = {
        10: analyzer.Stack(10, ["FirstRawMmap"]),
        11: analyzer.Stack(11, ["SecondRawMmap"]),
    }

    _output, summary, all_summary = analyzer.build_chrome_trace(
        [snapshot], lifecycle, stacks, top_n=10)

    self.assertEqual(sum(item["pss_bytes"] for item in summary), 12 * 1024)
    self.assertEqual(summary, all_summary)
    self.assertEqual(sum(item["rss_bytes"] for item in summary), 12 * 1024)
    self.assertEqual(sum(item["virtual_bytes"] for item in summary), 12 * 1024)
    self.assertEqual({item["pss_bytes"] for item in summary}, {6 * 1024})

  def test_build_chrome_trace_keeps_full_summary_for_classification(self):
    """分类数据源应等价于 --top-n 0，不受普通输出 top_n 截断。"""
    snapshot = analyzer.Snapshot(
        ts=2000,
        pid=1234,
        path="fake.smaps",
        vmas=[
            analyzer.SmapsVma(
                start=0x10000000,
                end=0x10001000,
                pathname="[anon:first]",
                rss_kb=4,
                pss_kb=4),
            analyzer.SmapsVma(
                start=0x20000000,
                end=0x20002000,
                pathname="[anon:second]",
                rss_kb=8,
                pss_kb=8),
        ])
    lifecycle = [
        (1500, "mmap", {
            "pid": 1234,
            "addr": 0x10000000,
            "size": 0x1000,
            "stack_id": 10,
            "path": "",
        }),
        (1501, "mmap", {
            "pid": 1234,
            "addr": 0x20000000,
            "size": 0x2000,
            "stack_id": 11,
            "path": "",
        }),
    ]
    stacks = {
        10: analyzer.Stack(10, ["FirstMmap"]),
        11: analyzer.Stack(11, ["SecondMmap"]),
    }

    output, summary, all_summary = analyzer.build_chrome_trace(
        [snapshot], lifecycle, stacks, top_n=1)

    self.assertEqual(len(summary), 1)
    self.assertEqual(len(output["metadata"]["final_summary"]), 1)
    self.assertEqual(len(all_summary), 2)
    self.assertEqual(
        {item["stack_id"] for item in all_summary},
        {10, 11})

  def test_mmap_classification_summary_matches_fs_rules(self):
    """mmap 分类应复用 fs.ini 规则，并按 PSS/RSS 等指标输出 remaining。"""
    rules = [
        analyzer.ClassificationRule("il2cpp/meta", ("Class::Init",)),
    ]
    summary_items = [
        {
            "stack_id": 10,
            "pss_bytes": 8 * 1024,
            "rss_bytes": 9 * 1024,
            "virtual_bytes": 10 * 1024,
            "private_dirty_bytes": 4 * 1024,
            "private_clean_bytes": 1 * 1024,
            "shared_dirty_bytes": 2 * 1024,
            "shared_clean_bytes": 3 * 1024,
            "range_count": 2,
            "stack": ["il2cpp::vm::Class::Init", "GameInit"],
            "stack_text": "il2cpp::vm::Class::Init\nGameInit",
        },
        {
            "stack_id": 11,
            "pss_bytes": 4 * 1024,
            "rss_bytes": 5 * 1024,
            "virtual_bytes": 6 * 1024,
            "private_dirty_bytes": 1 * 1024,
            "private_clean_bytes": 0,
            "shared_dirty_bytes": 0,
            "shared_clean_bytes": 0,
            "range_count": 1,
            "stack": ["OtherMmap"],
            "stack_text": "OtherMmap",
        },
    ]

    classified, remaining = analyzer.classify_mmap_summary_items(
        summary_items, rules)
    summary = analyzer.build_mmap_classification_summary(
        summary_items, classified, remaining)

    self.assertEqual(summary["stack_count"], 2)
    self.assertEqual(summary["pss_bytes"], 12 * 1024)
    self.assertEqual(summary["categories"][0]["name"], "il2cpp/meta")
    self.assertEqual(summary["categories"][0]["stack_count"], 1)
    self.assertEqual(summary["categories"][0]["pss_bytes"], 8 * 1024)
    self.assertEqual(summary["categories"][0]["rss_bytes"], 9 * 1024)
    self.assertEqual(summary["remaining"]["stack_count"], 1)
    self.assertEqual(summary["remaining"]["pss_bytes"], 4 * 1024)

  def test_attribute_snapshot_skips_ranges_that_cannot_overlap(self):
    """归因时应按地址跳过不可能重叠的 range，避免大 trace 卡在 JSON 生成。"""
    snapshot = analyzer.Snapshot(
        ts=2000,
        pid=1234,
        path="fake.smaps",
        vmas=[
            analyzer.SmapsVma(
                start=0x10000000,
                end=0x10001000,
                pathname="[anon:target]",
                rss_kb=4,
                pss_kb=4,
                private_dirty_kb=4)
        ])
    ranges = [
        analyzer.MmapRange(
            pid=1234,
            start=0x20000000 + index * 0x2000,
            end=0x20001000 + index * 0x2000,
            stack_id=20,
            mmap_ts=1000)
        for index in range(1000)
    ]
    ranges.append(analyzer.MmapRange(
        pid=1234,
        start=0x10000000,
        end=0x10001000,
        stack_id=10,
        mmap_ts=1000))

    checked_pairs = 0
    original_overlap_size = analyzer.overlap_size

    def count_overlap(*args):
      nonlocal checked_pairs
      checked_pairs += 1
      return original_overlap_size(*args)

    with mock.patch.object(analyzer, "overlap_size", side_effect=count_overlap):
      stats = analyzer.attribute_snapshot(snapshot, ranges)

    self.assertEqual(stats[10].pss_bytes, 4 * 1024)
    self.assertLess(checked_pairs, 20)

  def test_partial_munmap_splits_live_range(self):
    ranges = [
        analyzer.MmapRange(
            pid=1234,
            start=0x10000000,
            end=0x10004000,
            stack_id=10,
            mmap_ts=1000)
    ]

    ranges = analyzer.remove_overlap(
        ranges, pid=1234, start=0x10001000, size=0x1000)

    self.assertEqual(
        [(item.start, item.end) for item in ranges],
        [(0x10000000, 0x10001000), (0x10002000, 0x10004000)])

  def _fake_tp_query(self, _tp, _trace, sql):
    if "__intrinsic_perf_sample" in sql:
      return [{
          "ts": "1000",
          "utid": "7",
          "pid": "1234",
          "tid": "1234",
          "callsite_id": "10",
      }]

    if "stack_profile_symbol" in sql:
      return []

    if "__intrinsic_stack_profile_callsite" in sql:
      return [
          {
              "id": "10",
              "parent_id": "11",
              "frame_name": "AllocateByMmap",
              "deobfuscated_name": "",
              "mapping_name": "/data/app/libgame.so",
              "rel_pc": "16",
          },
          {
              "id": "11",
              "parent_id": "-1",
              "frame_name": "GameInit",
              "deobfuscated_name": "",
              "mapping_name": "/data/app/libgame.so",
              "rel_pc": "32",
          },
      ]

    if "__intrinsic_ftrace_event" in sql:
      return [
          # mmap enter: len = 16 KiB。
          {
              "event_id": "1",
              "ts": "1000",
              "utid": "7",
              "event_name": "raw_syscalls/sys_enter",
              "key": "id",
              "int_value": str(analyzer.ARM64_MMAP_NR),
              "string_value": "",
              "value_type": "int",
          },
          {
              "event_id": "1",
              "ts": "1000",
              "utid": "7",
              "event_name": "raw_syscalls/sys_enter",
              "key": "args[1]",
              "int_value": str(16 * 1024),
              "string_value": "",
              "value_type": "int",
          },
          # mmap exit: 返回起始地址 0x10000000。
          {
              "event_id": "2",
              "ts": "1100",
              "utid": "7",
              "event_name": "raw_syscalls/sys_exit",
              "key": "id",
              "int_value": str(analyzer.ARM64_MMAP_NR),
              "string_value": "",
              "value_type": "int",
          },
          {
              "event_id": "2",
              "ts": "1100",
              "utid": "7",
              "event_name": "raw_syscalls/sys_exit",
              "key": "ret",
              "int_value": str(0x10000000),
              "string_value": "",
              "value_type": "int",
          },
          # munmap enter: 释放前 4 KiB。
          {
              "event_id": "3",
              "ts": "1500",
              "utid": "7",
              "event_name": "raw_syscalls/sys_enter",
              "key": "id",
              "int_value": str(analyzer.ARM64_MUNMAP_NR),
              "string_value": "",
              "value_type": "int",
          },
          {
              "event_id": "3",
              "ts": "1500",
              "utid": "7",
              "event_name": "raw_syscalls/sys_enter",
              "key": "args[0]",
              "int_value": str(0x10000000),
              "string_value": "",
              "value_type": "int",
          },
          {
              "event_id": "3",
              "ts": "1500",
              "utid": "7",
              "event_name": "raw_syscalls/sys_enter",
              "key": "args[1]",
              "int_value": str(4 * 1024),
              "string_value": "",
              "value_type": "int",
          },
          # munmap exit: ret == 0 表示释放成功。
          {
              "event_id": "4",
              "ts": "1600",
              "utid": "7",
              "event_name": "raw_syscalls/sys_exit",
              "key": "id",
              "int_value": str(analyzer.ARM64_MUNMAP_NR),
              "string_value": "",
              "value_type": "int",
          },
          {
              "event_id": "4",
              "ts": "1600",
              "utid": "7",
              "event_name": "raw_syscalls/sys_exit",
              "key": "ret",
              "int_value": "0",
              "string_value": "",
              "value_type": "int",
          },
      ]

    raise AssertionError("未预期的 SQL: " + sql)


def subprocess_result(returncode, stdout, stderr):
  return analyzer.subprocess.CompletedProcess(
      args=["fake-tp"], returncode=returncode, stdout=stdout, stderr=stderr)


if __name__ == "__main__":
  unittest.main()
