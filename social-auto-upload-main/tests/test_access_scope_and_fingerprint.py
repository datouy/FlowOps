# -*- coding: utf-8 -*-
"""账号级 scope 真正生效 + 直连 HTTP 走账号指纹 UA/代理 的回归测试。

两个主题：

1. **scope 落地**：``runtime_config.in_scope`` / ``has_permission`` 过去是死代码，
   带 scope 的运营令牌照样能看到全部账号、全部发布任务和全部私信会话。
   这里验证账号 / 发布 / 私信三条链路的过滤与 403 拒绝行为。

2. **指纹一致性**：直连 HTTP（视频号私信 API、媒体下载）必须使用账号自己
   持久化的指纹 UA 和账号代理，否则同一账号会以两种设备身份出现在平台侧。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import access_scope  # noqa: E402
import runtime_config  # noqa: E402
from utils.account_http import (  # noqa: E402
    FALLBACK_USER_AGENT,
    account_headers,
    account_proxies,
    account_user_agent,
)
from utils.account_fingerprint import save_fingerprint  # noqa: E402


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
            (3, "dy-main.json", "抖音主号", 1),
            (3, "dy-other.json", "抖音他人号", 1),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    be = pytest.importorskip("sau_backend")
    _make_db(tmp_path)
    monkeypatch.setattr(be, "BASE_DIR", tmp_path, raising=True)
    # 账号范围判定必须跟着后端的 BASE_DIR 走，否则读到的是真实数据库。
    access_scope.bind_base_dir(lambda: be.BASE_DIR)
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", "")
    return be.app.test_client()


def _identity(role: str = "operator", scope=None) -> dict:
    permissions = {
        "admin": ("read", "publish", "message", "account", "team"),
        "operator": ("read", "publish", "message"),
        "viewer": ("read",),
    }[role]
    return {
        "user": "u",
        "role": role,
        "scope": list(scope or []),
        "permissions": list(permissions),
        "source": "user-token",
    }


def _use_identity(monkeypatch, identity):
    """让请求带上指定身份。

    后端的 ``_guard_access_token`` 会把 ``authenticate`` 的结果挂到 ``g.identity``，
    这里直接替换 ``authenticate``，避免往 app 上再挂 ``before_request``
    （app 收到第一个请求后就不能再注册了）。
    """
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "secret")
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", "")
    monkeypatch.setattr(runtime_config, "authenticate", lambda _token: identity)


# --------------------------------------------------------------- scope 生效

def test_local_mode_without_tokens_keeps_full_access(client):
    """未启用鉴权（本机单用户）时行为与改造前一致：全部账号可见。"""
    resp = client.get("/api/v2/accounts")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.get_json()["data"]["accounts"]]
    assert "抖音主号" in names and "抖音他人号" in names


def test_operator_scope_filters_account_list(client, monkeypatch):
    _use_identity(monkeypatch, _identity("operator", scope=["抖音主号"]))

    resp = client.get("/api/v2/accounts")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    names = [a["name"] for a in data["accounts"]]
    assert names == ["抖音主号"]
    # 汇总数字也必须按过滤后的集合统计，否则仍会泄露范围外元信息。
    assert data["stats"]["total"] == 1


def test_operator_scope_rejects_out_of_scope_account_detail(client, monkeypatch):
    _use_identity(monkeypatch, _identity("operator", scope=["抖音主号"]))

    resp = client.get("/api/v2/accounts/2")
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 403


def test_admin_scope_is_unrestricted(client, monkeypatch):
    _use_identity(monkeypatch, _identity("admin"))

    resp = client.get("/api/v2/accounts")
    names = [a["name"] for a in resp.get_json()["data"]["accounts"]]
    assert len(names) == 2


def test_viewer_cannot_publish(client, monkeypatch):
    _use_identity(monkeypatch, _identity("viewer", scope=["抖音主号"]))

    resp = client.post(
        "/postVideo",
        json={
            "title": "越权发布",
            "type": "3",
            "fileList": [],
            "accountList": ["dy-main.json"],
        },
    )
    assert resp.status_code == 403


def test_publish_to_out_of_scope_account_is_rejected(client, monkeypatch):
    _use_identity(monkeypatch, _identity("operator", scope=["抖音主号"]))

    resp = client.post(
        "/postVideo",
        json={
            "title": "越权发布",
            "type": "3",
            "fileList": [],
            "accountList": ["dy-other.json"],
        },
    )
    assert resp.status_code == 403
    assert "无权" in resp.get_json()["msg"]


def test_scope_accepts_account_id_as_well_as_name(client, monkeypatch):
    """scope 写账号 id 也要能生效。"""
    _use_identity(monkeypatch, _identity("operator", scope=["1"]))

    resp = client.get("/api/v2/accounts")
    names = [a["name"] for a in resp.get_json()["data"]["accounts"]]
    assert names == ["抖音主号"]


def test_scope_accepts_cookie_file_name(client, monkeypatch):
    _use_identity(monkeypatch, _identity("operator", scope=["dy-other.json"]))

    resp = client.get("/api/v2/accounts")
    names = [a["name"] for a in resp.get_json()["data"]["accounts"]]
    assert names == ["抖音他人号"]


def test_download_cookie_out_of_scope_is_rejected(client, monkeypatch, tmp_path):
    _use_identity(monkeypatch, _identity("operator", scope=["dy-main.json"]))
    cookie = client.application  # 仅确保 app 已加载
    assert cookie is not None

    resp = client.get("/downloadCookie?filePath=dy-other.json")
    assert resp.status_code == 403


def test_upload_cookie_requires_account_permission(client, monkeypatch):
    _use_identity(monkeypatch, _identity("operator", scope=["dy-main.json"]))

    resp = client.post("/uploadCookie", data={})
    assert resp.status_code == 403


# --------------------------------------------------------------- 指纹一致性

def test_account_user_agent_uses_persisted_fingerprint(tmp_path):
    cookie = tmp_path / "cookiesFile" / "a.json"
    cookie.parent.mkdir(parents=True, exist_ok=True)
    cookie.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    windows_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    save_fingerprint(cookie, {"user_agent": windows_ua, "device_id": "dev-1"})

    assert account_user_agent(cookie) == windows_ua
    assert account_headers(cookie)["User-Agent"] == windows_ua


def test_account_user_agent_falls_back_when_no_fingerprint(tmp_path):
    cookie = tmp_path / "a.json"
    cookie.write_text("{}", encoding="utf-8")

    assert account_user_agent(cookie) == FALLBACK_USER_AGENT
    assert account_user_agent(None) == FALLBACK_USER_AGENT


def test_account_proxies_uses_account_proxy(tmp_path, monkeypatch):
    from utils import account_http

    cookie = tmp_path / "a.json"
    cookie.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        account_http,
        "proxy_settings",
        lambda _path: {
            "server": "http://127.0.0.1:8888",
            "username": "user",
            "password": "p@ss",
        },
    )

    proxies = account_proxies(cookie)
    assert proxies["http"] == "http://user:p%40ss@127.0.0.1:8888"
    assert proxies["https"] == proxies["http"]


def test_account_proxies_empty_without_proxy(tmp_path):
    cookie = tmp_path / "a.json"
    cookie.write_text("{}", encoding="utf-8")
    assert account_proxies(cookie) == {}


def test_channels_client_uses_account_fingerprint_ua(tmp_path, monkeypatch):
    """视频号直连 API 必须用账号指纹 UA，而不是写死的 Linux UA。"""
    import channels_dm

    cookie = tmp_path / "cookiesFile" / "ch.json"
    cookie.parent.mkdir(parents=True, exist_ok=True)
    cookie.write_text(
        json.dumps({"cookies": [{"name": "wxuin", "value": "9", "domain": ".qq.com"}]}),
        encoding="utf-8",
    )
    windows_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    save_fingerprint(cookie, {"user_agent": windows_ua})

    monkeypatch.setattr(channels_dm, "account_proxies", lambda _path: {})
    client = channels_dm.ChannelsClient(
        {"cookie_path": str(cookie), "name": "视频号主号"}
    )

    assert client._user_agent == windows_ua
    headers = channels_dm._headers(client._wxuin, client._user_agent)
    assert headers["User-Agent"] == windows_ua


def test_dm_media_cache_download_uses_account_headers(tmp_path, monkeypatch):
    """媒体下载必须带账号指纹 UA 并走账号代理。"""
    import dm_media_cache

    cookie = tmp_path / "cookiesFile" / "m.json"
    cookie.parent.mkdir(parents=True, exist_ok=True)
    cookie.write_text("{}", encoding="utf-8")
    windows_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    save_fingerprint(cookie, {"user_agent": windows_ua})

    captured: dict = {}

    class Response:
        status_code = 200
        headers = {"Content-Type": "image/png"}
        content = b"png"

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            yield b"png"

        def close(self):
            return None

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return Response()

    monkeypatch.setattr(dm_media_cache.requests, "get", fake_get)
    monkeypatch.setattr(
        dm_media_cache,
        "account_proxies",
        lambda _path: {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"},
    )

    target, content_type = dm_media_cache.download_media(
        "https://cdn.example/a.png", tmp_path / "media", "stem",
        cookie_path=cookie,
    )
    assert target.is_file()
    assert content_type == "image/png"
    assert captured["headers"]["User-Agent"] == windows_ua
    assert captured["proxies"]["http"] == "http://127.0.0.1:1080"


# --------------------------------------------------------------- 私信侧 scope

@pytest.fixture()
def dm_client(tmp_path, monkeypatch):
    """私信工作台：账号表里放两个抖音号，私信里各有一条会话。

    返回 ``(test_client, dm_ops, set_identity)``——``set_identity`` 用来在
    请求前切换当前身份（私信蓝图没有后端那层令牌守卫）。
    """
    from flask import Flask, g

    import dm_ops

    _make_db(tmp_path)
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    # 账号表指向临时库：active_account_pairs 会从里面读已登录账号。
    monkeypatch.setattr(dm_ops, "ACCOUNT_DB", tmp_path / "db" / "database.db")
    monkeypatch.setattr(access_scope, "bind_base_dir", lambda _provider: None)
    access_scope.bind_base_dir(lambda: tmp_path)
    dm_ops.init_db()
    app = Flask(__name__)
    app.register_blueprint(dm_ops.bp)
    state = {"identity": None}

    @app.before_request
    def _inject():
        g.identity = state["identity"]

    def set_identity(identity):
        state["identity"] = identity

    return app.test_client(), dm_ops, set_identity


def _ingest(dm_ops, account: str, external_id: str) -> dict:
    return dm_ops.ingest_message({
        "platform": "抖音",
        "account": account,
        "external_user_id": "customer-1",
        "external_message_id": external_id,
        "author": "客户",
        "nickname": "客户",
        "content": "你好",
        "direction": "inbound",
        "created_at": "2026-11-04 17:00:00",
    }, allow_auto_reply=False)


def test_dm_conversation_list_is_scoped(dm_client):
    client, dm_ops, set_identity = dm_client
    _ingest(dm_ops, "抖音主号", "m1")
    _ingest(dm_ops, "抖音他人号", "m2")

    set_identity(_identity("operator", scope=["抖音主号"]))

    listing = client.get("/dm/conversations").get_json()["data"]
    assert [row["account"] for row in listing] == ["抖音主号"]


def test_dm_conversation_list_unrestricted_without_identity(dm_client):
    """未启用鉴权（身份为 None）时行为与改造前一致。"""
    client, dm_ops, set_identity = dm_client
    _ingest(dm_ops, "抖音主号", "m1")
    _ingest(dm_ops, "抖音他人号", "m2")

    set_identity(None)

    listing = client.get("/dm/conversations").get_json()["data"]
    assert {row["account"] for row in listing} == {"抖音主号", "抖音他人号"}


def test_dm_reply_on_out_of_scope_conversation_is_rejected(dm_client):
    client, dm_ops, set_identity = dm_client
    item = _ingest(dm_ops, "抖音他人号", "m2")

    set_identity(_identity("operator", scope=["抖音主号"]))

    resp = client.post(
        f"/dm/conversations/{item['contact_id']}/reply",
        json={"text": "越权回复"},
    )
    assert resp.status_code == 403


def test_dm_detail_on_out_of_scope_conversation_is_rejected(dm_client):
    client, dm_ops, set_identity = dm_client
    item = _ingest(dm_ops, "抖音他人号", "m2")

    set_identity(_identity("operator", scope=["抖音主号"]))

    resp = client.get(f"/dm/conversations/{item['contact_id']}")
    assert resp.status_code == 403


def test_dm_reply_in_scope_conversation_is_allowed(dm_client):
    client, dm_ops, set_identity = dm_client
    item = _ingest(dm_ops, "抖音主号", "m1")

    set_identity(_identity("operator", scope=["抖音主号"]))

    resp = client.post(
        f"/dm/conversations/{item['contact_id']}/reply",
        json={"text": "正常回复"},
    )
    assert resp.status_code == 200


def test_manual_reply_persists_and_returns_delivery_failure(dm_client, monkeypatch):
    client, dm_ops, _set_identity = dm_client
    item = _ingest(dm_ops, "抖音主号", "m-failure")
    import platform_reply

    def fail_delivery(*_args, **_kwargs):
        raise RuntimeError("账号浏览器目录正被其他任务占用")

    monkeypatch.setattr(platform_reply, "send_douyin", fail_delivery)
    resp = client.post(
        f"/dm/conversations/{item['contact_id']}/reply",
        json={"text": "失败也要留痕"},
    )
    payload = resp.get_json()["data"]
    assert resp.status_code == 200
    assert payload["delivery_status"] == "failed"
    assert "账号浏览器目录正被其他任务占用" in payload["delivery_error"]

    detail = client.get(f"/dm/conversations/{item['contact_id']}").get_json()["data"]
    message = next(row for row in detail["messages"] if row["id"] == payload["outbound_message_id"])
    assert message["delivery_status"] == "failed"
    assert "账号浏览器目录正被其他任务占用" in message["delivery_error"]
    assert detail["logs"][0]["outbound_message_id"] == payload["outbound_message_id"]
