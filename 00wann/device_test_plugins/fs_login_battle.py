"""FS 登录、表加载和战斗录像测试流程。"""

from device_test_framework.flows.base import FlowSpec


def create_flow(action_script: str) -> FlowSpec:
  return FlowSpec(
      name="fs_login_battle",
      action_script=action_script,
      environment_values={"PERF_PROFILE_ACTION_SCRIPT": action_script},
  )
