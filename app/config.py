"""运行期配置。

路径类配置通过函数在调用时读取环境变量，便于测试中用 monkeypatch 切换数据库。
"""
from __future__ import annotations

import os
from pathlib import Path

# 写事务等待锁的最长时间（毫秒），配合 BEGIN IMMEDIATE 实现并发写串行化。
BUSY_TIMEOUT_MS = int(os.environ.get("NOISE_BUSY_TIMEOUT_MS", "10000"))

# 小时区间查询最多返回的小时数，防止误传超大区间。
MAX_HOUR_RANGE = int(os.environ.get("NOISE_MAX_HOUR_RANGE", str(24 * 31)))


def get_db_path() -> Path:
    """SQLite 数据库文件路径，docker compose 通过环境变量指向挂载卷。"""
    return Path(os.environ.get("NOISE_DB_PATH", "/data/noise.db"))
