#!/usr/bin/env python3
"""00wann 真机性能测试宿主插件测试。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from device_test_framework.config import (
    FrameworkConfig,
    RpcConfig,
    ToolConfig,
    load_framework_config,
)
from device_test_framework.flows.base import FlowSpec
from device_test_framework.models import RunContext
from device_test_framework.platforms.android import AndroidAdapter
from device_test_plugins.environment import (
    LEGACY_ENVIRONMENT,
    initialize_project_environment,
)
from device_test_plugins.fs_login_battle import create_flow
from device_test_plugins.malloc import MallocFeature
from device_test_plugins.mmap import MmapFeature
from device_test_plugins.none import create_flow as create_no_flow
from device_test_plugins.registry import REGISTRY


class ProjectConfigTest(unittest.TestCase):

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    self.root = Path(self.temp_dir.name)
    (self.root / "device_test.ini").write_text(
        "[run]\nplatform=android\nflow=none\noutput_root=data\n"
        "[device]\nid=ini-device\n"
        "[target]\napp_id=ini.app\nlaunch_id=ini.app/.Main\n"
        "[tools]\nperfetto_root=perfetto\n",
        encoding="utf-8")

  def tearDown(self):
    self.temp_dir.cleanup()

  def test旧环境变量由宿主映射覆盖ini(self):
    config = load_framework_config(
        self.root,
        "mmap",
        [],
        environ={
            "ANDROID_SERIAL": "legacy-device",
            "MMAP_PHYS_APP": "com.tencent.dhwdxkty.trunk.profiler",
            "MMAP_PHYS_ACTIVITY": "com.tencent.dhwdxkty.trunk.profiler/.Main",
            "PerfettoRoot": "legacy-perfetto",
        },
        legacy_environment=LEGACY_ENVIRONMENT,
    )
    self.assertEqual(config.device_id, "legacy-device")
    self.assertEqual(config.app_id, "com.tencent.dhwdxkty.trunk.profiler")
    self.assertEqual(
        config.launch_id, "com.tencent.dhwdxkty.trunk.profiler/.Main")
    self.assertEqual(
        config.tools.perfetto_root,
        (self.root / "legacy-perfetto").resolve())

  def test一次性初始化从最终包名派生activity(self):
    environ = {
        "MMAP_PHYS_APP": "com.tencent.dhwdxkty.trunk.profiler",
    }
    with mock.patch.dict(os.environ, environ, clear=True):
      initialize_project_environment()
      self.assertEqual(
          os.environ["DEVICE_TEST_LAUNCH_ID"],
          "com.tencent.dhwdxkty.trunk.profiler/"
          "com.dhplugin.unity.MainActivity")

  def test新包名覆盖旧包名时activity保持同一目标(self):
    environ = {
        "DEVICE_TEST_APP_ID": "com.example.override",
        "MMAP_PHYS_APP": "com.tencent.dhwdxkty.trunk.profiler",
    }
    with mock.patch.dict(os.environ, environ, clear=True):
      initialize_project_environment()
      self.assertEqual(
          os.environ["DEVICE_TEST_LAUNCH_ID"],
          "com.example.override/com.dhplugin.unity.MainActivity")


class ProjectFeatureTest(unittest.TestCase):

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    root = Path(self.temp_dir.name)
    self.config = FrameworkConfig(
        project_root=root,
        config_path=root / "device_test.ini",
        platform="android",
        feature="malloc",
        flow="fs_login_battle",
        device_id="serial",
        app_id="com.tencent.dhwdxkty.trunk.profiler",
        launch_id=(
            "com.tencent.dhwdxkty.trunk.profiler/"
            "com.dhplugin.unity.MainActivity"),
        output_root=root / "PerfData",
        action_script="profile_actions/send_battle_record_gm.py",
        tools=ToolConfig(
            root / "perfetto", backend_python="backend-python"),
        rpc=RpcConfig(local_port=12346, timeout_seconds=12),
        feature_options={
            "interval_bytes": "4096",
            "shmem_size_bytes": "16777216",
            "buffer_kib": "131072",
        },
    )
    self.platform = mock.Mock()
    self.platform.capabilities = AndroidAdapter.capabilities
    self.platform.environment.return_value = {}
    self.context = RunContext(
        self.config,
        root / "PerfData" / "result",
        self.platform,
        create_flow(self.config.action_script),
    )

  def tearDown(self):
    self.temp_dir.cleanup()

  def testMalloc参数输出目录和项目环境传给后端(self):
    feature = MallocFeature()
    command = feature.build_command(self.context)
    environment = feature.build_environment(self.context)
    self.assertEqual(command[0], "backend-python")
    self.assertEqual(command[-2:], ["4096", "16777216"])
    self.assertEqual(
        environment["DEVICE_TEST_OUTPUT_DIR"], str(self.context.output_dir))
    self.assertEqual(environment["MMAP_PHYS_APP"], self.config.app_id)
    self.assertEqual(environment["MMAP_PHYS_ACTIVITY"], self.config.launch_id)
    self.assertEqual(environment["HEAP_PROFILE_RPC_LOCAL_PORT"], "12346")
    self.assertEqual(environment["HEAP_PROFILE_RPC_TIMEOUT_S"], "12")
    self.assertEqual(
        environment["PERF_PROFILE_ACTION_SCRIPT"], self.config.action_script)

  def testMmap默认参数在用户参数之前且工具映射正确(self):
    tool_dir = self.config.tools.perfetto_root / (
        "out/win_clang" if os.name == "nt" else "out/linux_clang_release")
    tool_dir.mkdir(parents=True)
    processor_name = (
        "trace_processor_shell.exe" if os.name == "nt"
        else "trace_processor_shell")
    (tool_dir / processor_name).write_text("测试工具\n", encoding="utf-8")
    config = FrameworkConfig(
        **{
            **self.config.__dict__,
            "feature": "mmap",
            "backend_args": ("--top-n", "25"),
        })
    context = RunContext(
        config, self.context.output_dir, self.platform,
        create_flow(config.action_script))
    command = MmapFeature().build_command(context)
    self.assertIn("collect_mmap_phys_data.py", command[1])
    self.assertEqual(command[command.index("--buffer-kb") + 1], "131072")
    self.assertTrue(
        command[command.index("--trace-processor") + 1].endswith(
            processor_name))
    top_indices = [
        index for index, value in enumerate(command) if value == "--top-n"]
    self.assertEqual(
        [command[index + 1] for index in top_indices], ["0", "25"])

  def testMmap平台能力和Fs文件在采集前校验(self):
    self.platform.capabilities = frozenset({"perfetto"})
    result = MmapFeature().validate(self.context)
    self.assertFalse(result.success)
    self.assertIn("app_lifecycle", result.error)
    self.platform.capabilities = AndroidAdapter.capabilities
    result = MmapFeature().validate(self.context)
    self.assertIn("mmap_boot_config_missing", result.error)
    (self.config.project_root / "FSBootCmdLine.cfg").write_text(
        "测试启动配置\n", encoding="utf-8")
    result = MmapFeature().validate(self.context)
    self.assertIn("mmap_debug_config_missing", result.error)
    (self.config.project_root / "debugconfig.txt").write_text(
        "测试调试配置\n", encoding="utf-8")
    self.assertTrue(MmapFeature().validate(self.context).success)


class RegistryAndFlowTest(unittest.TestCase):

  def test项目只显式注册现有平台功能和流程(self):
    self.assertEqual(set(REGISTRY.platforms), {"android"})
    self.assertEqual(set(REGISTRY.features), {"malloc", "mmap"})
    self.assertEqual(set(REGISTRY.flows), {"fs_login_battle", "none"})

  def testFs流程输出测试模块而空流程禁用操作(self):
    flow = create_flow("profile_actions/test.py")
    self.assertEqual(
        flow.environment()["PERF_PROFILE_ACTION_SCRIPT"],
        "profile_actions/test.py")
    none = create_no_flow("ignored.py")
    self.assertIsInstance(none, FlowSpec)
    self.assertFalse(none.enabled)
    self.assertNotIn("PERF_PROFILE_ACTION_SCRIPT", none.environment())


if __name__ == "__main__":
  unittest.main()
