"""00wann 平台、采集功能和测试流程注册表。"""

from device_test_framework.platforms.android import AndroidAdapter
from device_test_framework.registry import Registry

from .fs_login_battle import create_flow as create_fs_login_battle
from .malloc import MallocFeature
from .mmap import MmapFeature
from .none import create_flow as create_no_flow


REGISTRY = Registry(
    platforms={"android": AndroidAdapter},
    features={"malloc": MallocFeature, "mmap": MmapFeature},
    flows={"fs_login_battle": create_fs_login_battle, "none": create_no_flow},
)
