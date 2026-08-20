"""00wann 平台、采集功能和 Profile Action 注册表。"""

from profile_actions import PROFILE_ACTIONS
from device_test_framework.platforms.android import AndroidAdapter
from device_test_framework.registry import Registry

from .malloc import MallocFeature
from .mmap import MmapFeature


REGISTRY = Registry(
    platforms={"android": AndroidAdapter},
    features={"malloc": MallocFeature, "mmap": MmapFeature},
    profile_actions=PROFILE_ACTIONS,
)
