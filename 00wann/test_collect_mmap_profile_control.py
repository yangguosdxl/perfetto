#!/usr/bin/env python3
"""mmap 主调用栈采集阶段控制单元测试。"""

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import collect_mmap_phys_data as collector
from profile_action_runner import ProfileActionResult


class MmapProfileControlTest(unittest.TestCase):

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    self.output_dir = Path(self.temp_dir.name) / "output"
    self.args = types.SimpleNamespace(
        output=str(self.output_dir),
        name="com.example.app",
        buffer_kb=1024,
        no_ftrace=False,
        no_kernel_frames=False,
        perf_ring_buffer_pages=8,
        perf_ring_buffer_read_period_ms=25,
        no_guardrails=False,
        wait_timeout_s=10,
        smaps_interval_ms=10,
        use_su=False,
    )

  def tearDown(self):
    self.temp_dir.cleanup()

  def test_root_producer和perfetto先于app且由测试模块结束(self):
    events = []

    def start_perfetto(config, _device_trace, _no_guardrails):
      self.assertNotIn("duration_ms:", config)
      if 'name: "linux.perf"' in config:
        events.append("callstack_perfetto_start")
        return 78
      events.append("lifecycle_perfetto_start")
      return 77

    def collect_smaps(*_args, stop_event=None, **_kwargs):
      events.append("smaps_start")
      self.assertIsNotNone(stop_event)
      stop_event.wait(1)
      events.append("smaps_stop")

    def wait_log(_path, _pid, pattern, _timeout):
      events.append("login" if pattern == collector.LOGIN_DONE_PATTERN else "table")
      return "ready"

    def run_action(_module, context, _stop_requested, _process_alive):
      events.append("action")
      self.assertEqual(context.pid, 4321)
      return ProfileActionResult(True, "action_completed", None)

    patches = (
        mock.patch.object(
            collector, "start_root_traced_perf",
            side_effect=lambda: events.append("root_start") or None),
        mock.patch.object(collector, "start_perfetto", side_effect=start_perfetto),
        mock.patch.object(collector, "force_stop_app",
                          side_effect=lambda _name: events.append("app_stop")),
        mock.patch.object(collector, "start_logcat_capture",
                          return_value=(None, None, None)),
        mock.patch.object(collector, "wait_for_pid",
                          side_effect=lambda *_args: events.append("app_start") or 4321),
        mock.patch.object(collector, "collect_smaps", side_effect=collect_smaps),
        mock.patch.object(collector, "wait_for_app_log_pattern", side_effect=wait_log),
        mock.patch.object(collector, "run_profile_action_module", side_effect=run_action),
        mock.patch.object(collector, "stop_logcat_capture"),
        mock.patch.object(collector, "stop_perfetto",
                          side_effect=lambda _pid: events.append("perfetto_stop")),
        mock.patch.object(collector, "finish_collection",
                          return_value={"status": 0}),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
      result = collector.run_profile_controlled_collection(
          self.args, types.SimpleNamespace())

    self.assertEqual(result["status"], 0)
    self.assertLess(
        events.index("root_start"), events.index("lifecycle_perfetto_start"))
    self.assertLess(
        events.index("lifecycle_perfetto_start"), events.index("app_stop"))
    self.assertLess(
        events.index("app_start"), events.index("callstack_perfetto_start"))
    self.assertLess(
        events.index("callstack_perfetto_start"), events.index("smaps_start"))
    self.assertLess(events.index("app_start"), events.index("smaps_start"))
    self.assertLess(events.index("login"), events.index("table"))
    self.assertLess(events.index("table"), events.index("action"))
    self.assertLess(events.index("action"), events.index("smaps_stop"))
    self.assertLess(events.index("smaps_stop"), events.index("perfetto_stop"))

  def test_root_producer状态在perfetto启动失败时仍恢复(self):
    state = collector.RootTracedPerfState(88, "1", "running")
    with mock.patch.object(
        collector, "start_root_traced_perf", return_value=state), \
         mock.patch.object(
             collector, "start_perfetto", side_effect=RuntimeError("启动失败")), \
         mock.patch.object(collector, "stop_root_traced_perf") as stop_root, \
         mock.patch.object(collector, "finish_collection",
                           side_effect=RuntimeError("没有 trace")):
      result = collector.run_profile_controlled_collection(
          self.args, types.SimpleNamespace())

    self.assertEqual(result["status"], 1)
    stop_root.assert_called_once_with(state)

  def test_smaps_线程失败会让本轮失败(self):
    with mock.patch.object(collector, "start_root_traced_perf", return_value=None), \
         mock.patch.object(collector, "start_perfetto", return_value=77), \
         mock.patch.object(collector, "force_stop_app"), \
         mock.patch.object(collector, "start_logcat_capture",
                           return_value=(None, None, None)), \
         mock.patch.object(collector, "wait_for_pid", return_value=4321), \
         mock.patch.object(collector, "collect_smaps",
                           side_effect=RuntimeError("smaps 测试失败")), \
         mock.patch.object(collector, "wait_for_app_log_pattern",
                           return_value="ready"), \
         mock.patch.object(collector, "run_profile_action_module",
                           return_value=ProfileActionResult(
                               True, "action_completed", None)), \
         mock.patch.object(collector, "stop_logcat_capture"), \
         mock.patch.object(collector, "stop_perfetto"), \
         mock.patch.object(collector, "finish_collection",
                           return_value={"status": 0}):
      result = collector.run_profile_controlled_collection(
          self.args, types.SimpleNamespace())
    self.assertEqual(result["status"], 1)


if __name__ == "__main__":
  unittest.main()
