"""不执行登录后测试操作的空流程。"""

from device_test_framework.flows.base import FlowSpec


def create_flow(_action_script: str) -> FlowSpec:
  return FlowSpec(name="none", action_script="", enabled=False)
