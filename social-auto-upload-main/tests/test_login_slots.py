# -*- coding: utf-8 -*-
"""登录账号选择（/accounts/slots 与覆盖登录守卫）回归测试。

覆盖三件事：
1. 平台归一化：douyin/xiaohongshu/tencent、中文名、type 编号都要认；
2. 槽位列表只返回本平台账号，并给出建议新名称；
3. 覆盖登录的守卫：账号不存在 → 404，跨平台覆盖 → 400（在启动浏览器之前拦下）。

测试用临时目录替换 sau_backend.BASE_DIR，不碰真实数据库。
导入 sau_backend 会初始化整个 app（后台线程、patchright 驱动），
因此这里只打接口，不启动浏览器。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_db(base: Path) -> None:
    (base / "db").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(base / "db" / "database.db")
    conn.execute(
        "CREATE TABLE user_info ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " type INTEGER, filePath TEXT, userName TEXT, status INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO user_info (type, filePath, userName, status) VALUES (?,?,?,?)",
        [
            (1, "xhs1.json", "小红书主号", 1),
            (1, "xhs2.json", "小红书小号", 0),
            (3, "dy1.json", "抖音主号", 1),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    be = pytest.importorskip("sau_backend")
    import runtime_config

    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    _make_db(tmp_path)
    monkeypatch.setattr(be, "BASE_DIR", tmp_path, raising=True)
    return be.app.test_client()


def test_slots_by_key(client):
    resp = client.get("/accounts/slots?platform=xiaohongshu")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["platform"] == {"key": "xiaohongshu", "type": 1, "label": "小红书"}
    assert [a["name"] for a in data["accounts"]] == ["小红书主号", "小红书小号"]
    assert data["accounts"][1]["statusText"] == "异常"
    assert data["nextName"] == "小红书账号3"


def test_slots_accepts_label_and_type(client):
    assert client.get("/accounts/slots?platform=视频号").get_json()["data"]["platform"]["key"] == "tencent"
    assert client.get("/accounts/slots?type=3").get_json()["data"]["platform"]["key"] == "douyin"


def test_slots_rejects_unsupported_platform(client):
    resp = client.get("/accounts/slots?platform=kuaishou")
    assert resp.status_code == 400


def test_replace_login_requires_existing_account(client):
    resp = client.post(
        "/interactiveLogin/start",
        json={"id": "x", "platform": "xiaohongshu", "account_id": 9999},
    )
    assert resp.status_code == 404


def test_replace_login_rejects_cross_platform(client):
    resp = client.post(
        "/interactiveLogin/start",
        json={"id": "x", "platform": "xiaohongshu", "account_id": 3},
    )
    assert resp.status_code == 400
    assert "不属于" in resp.get_json()["msg"]
