from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 单测是"隔离文件系统"的：它们造的是假账号名 + 不存在的素材文件，发布前预检
# 一定会拦下来（这正是它的职责）。所以测试环境默认关闭预检，预检本身由
# tests/test_runtime_stability.py 直接调函数验证。真实运行默认开启。
os.environ.setdefault("FLOWOPS_PUBLISH_PREFLIGHT", "0")
