"""pytest 公共 fixtures 与路径配置"""

from __future__ import annotations

import os
import sys

import pytest

# 将项目根目录加入 sys.path,使 `from src.xxx` 在 tests 中可用
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
