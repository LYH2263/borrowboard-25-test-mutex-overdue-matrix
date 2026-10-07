import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.tests.world_factory import World


@pytest.fixture
def world(tmp_path, monkeypatch):
    """每个用例一个独立 sqlite 文件、一个带 startup 建表的 TestClient。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        yield World(client)
