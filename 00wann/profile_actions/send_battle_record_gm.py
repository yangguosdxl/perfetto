#!/usr/bin/env python3
"""默认测试操作：登录表就绪后通过 Poco RPC 发送战斗录像 GM。"""

from __future__ import annotations

import os

from profile_action_api import ProfileActionSession

GM_COMMAND = "CheatFunc_BatchCheatOption.战斗录像:@40011@@|"


def get_collection_wait_seconds(_session: ProfileActionSession) -> float:
  # 保留旧环境变量，便于现有自动测试缩短等待时间。
  return float(os.environ.get("HEAP_PROFILE_LOGIN_STABLE_S", "120"))


async def run_profile_action(session: ProfileActionSession) -> None:
  result = await session.invoke_rpc("DoRecordCheat", [GM_COMMAND])
  if not result.success:
    # 公共接口只返回失败；默认测试策略选择让本轮采集失败。
    raise RuntimeError(result.error)
  await session.wait_forever()
