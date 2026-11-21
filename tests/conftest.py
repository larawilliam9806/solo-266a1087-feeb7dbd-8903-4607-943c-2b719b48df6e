"""测试夹具：每个用例独立临时数据库。"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    tmpdir = tempfile.mkdtemp(prefix="noise-test-")
    db_path = os.path.join(tmpdir, "test.db")
    monkeypatch.setenv("NOISE_DB_PATH", db_path)

    # 延迟导入，确保 config 读取到被 patch 的环境变量。
    from app import database
    from app.config import get_db_path

    assert str(get_db_path()) == db_path
    database.init_db(db_path)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
