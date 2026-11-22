"""pytest 公共夹具：每个用例独立的临时 SQLite 库 + TestClient。"""

from __future__ import annotations

import itertools
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="fall-approval-test-")
_counter = itertools.count()

from app import db as db_mod  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client():
    # 每用例独立库文件；lifespan 与请求依赖都按 DEFAULT_DB_PATH 打开
    db_path = os.path.join(_tmpdir, f"case-{next(_counter)}.db")
    db_mod.DEFAULT_DB_PATH = db_path
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
