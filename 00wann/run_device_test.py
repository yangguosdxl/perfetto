#!/usr/bin/env python3
"""00wann 真机性能测试宿主入口。"""

from pathlib import Path

from device_test_framework.cli import main
from device_test_plugins.environment import (
    LEGACY_ENVIRONMENT,
    initialize_project_environment,
)
from device_test_plugins.registry import REGISTRY


if __name__ == "__main__":
  initialize_project_environment()
  raise SystemExit(main(
      REGISTRY,
      project_root=Path(__file__).resolve().parent,
      legacy_environment=LEGACY_ENVIRONMENT,
  ))
