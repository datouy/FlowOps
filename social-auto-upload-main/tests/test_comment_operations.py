# -*- coding: utf-8 -*-
"""评论运营能力（回复 / 点赞 / 删除 / 置顶）的测试。

覆盖三件事：

1. **动作分发**：四个动作各自路由到正确的适配器方法；
2. **前置校验**：缺少平台评论 ID / 作品 ID、平台不支持、内容为空时明确拒绝，
   而不是"假装成功"；
3. **接口层**：范围外的评论返回 403，范围外的账号不出现在列表里，操作成功后
   状态回写到 ``comment_items``。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import access_scope  # noqa: E402
import runtime_config  # noqa: E402


# ---------------------------------------------------------------- 动作分发

def _comment(**overrides):
    payload = {
        "platform": "视频号",
        "account": "视频号主号",
        "comment_id": "c-100",
        "source_id": "post-200",
        "source_title": "三亚五日游",
        "author": "小明",
        "content": "请问多少钱",
    }
    payload.update(overrides)
    return payload


def test_platform_type_of_accepts_name_and_number():
    comment_ops = pytest.importorskip("comment_ops")
    assert comment_ops.platform_type_of("抖音") == 3
    assert comment_ops.platform_type_of("视频号") == 2
    assert comment_ops.platform_type_of(1) == 1
    assert comment_ops.platform_type_of("") == 0
    assert comment_ops.platform_type_of(None) == 0


def test_all_four_actions_are_supported():
    """文档要求的四个动作必须全部在册。"""
    comment_ops = pytest.importorskip("comment_ops")
    for action in ("reply", "like", "delete", "pin"):
        assert action in comment_ops.COMMENT_ACTIONS
    # 取消点赞 / 取消置顶是同一动作的反向操作态。
    assert "unlike" in comment_ops.COMMENT_ACTIONS
    assert "unpin" in comment_ops.COMMENT_ACTIONS


def test_platform_capabilities_cover_supported_platforms():
    comment_ops = pytest.importorskip("comment_ops")
    matrix = {item["name"]: item for item in comment_ops.platform_capabilities()}
    for name in ("小红书", "视频号", "抖音", "快手"):
        assert matrix[name]["supported"] is True
        assert "reply" in matrix[name]["actions"]
        assert "pin" in matrix[name]["actions"]
    # 视频号走接口，其余走页面自动化。
    assert matrix["视频号"]["transport"] == "api"
    assert matrix["抖音"]["transport"] == "dom"


def test_unsupported_action_is_rejected():
    comment_ops = pytest.importorskip("comment_ops")
    with pytest.raises(comment_ops.CommentActionError):
        comment_ops.run_comment_action(
            {"cookie_path": ""}, _comment(), "explode", text="x",
        )


def test_reply_requires_content():
    comment_ops = pytest.importorskip("comment_ops")
    with pytest.raises(comment_ops.CommentActionError) as excinfo:
        comment_ops.run_comment_action({"cookie_path": ""}, _comment(), "reply", text="  ")
    assert "回复内容不能为空" in str(excinfo.value)


def test_missing_comment_id_is_rejected():
    comment_ops = pytest.importorskip("comment_ops")
    with pytest.raises(comment_ops.CommentActionError) as excinfo:
        comment_ops.run_comment_action(
            {"cookie_path": ""}, _comment(comment_id=""), "like",
        )
    assert "评论 ID" in str(excinfo.value)


def test_missing_source_id_is_rejected():
    """缺少所属作品 ID 时无法在平台上定位评论，必须明确拒绝。"""
    comment_ops = pytest.importorskip("comment_ops")
    with pytest.raises(comment_ops.CommentActionError) as excinfo:
        comment_ops.run_comment_action(
            {"cookie_path": ""}, _comment(source_id=""), "like",
        )
    assert "作品 ID" in str(excinfo.value)


def test_unknown_platform_is_rejected():
    comment_ops = pytest.importorskip("comment_ops")
    with pytest.raises(comment_ops.CommentActionError) as excinfo:
        comment_ops.run_comment_action(
            {"cookie_path": ""}, _comment(platform="火星"), "like",
        )
    assert "平台" in str(excinfo.value)


def test_channels_actions_route_to_api(monkeypatch):
    """视频号的四个动作都要打到对应的接口路径上。"""
    comment_ops = pytest.importorskip("comment_ops")
    calls = []

    class FakeClient:
        def _request(self, path, body=None, api_base=None):
            calls.append((path, body or {}, api_base))
            return {"errCode": 0}

        def close(self):
            pass

    monkeypatch.setattr(
        comment_ops.ChannelsCommentAdapter, "_client", lambda self: FakeClient(),
    )
    account = {"cookie_path": "", "id": 1}
    comment_ops.run_comment_action(account, _comment(), "reply", text="您好")
    comment_ops.run_comment_action(account, _comment(), "like")
    comment_ops.run_comment_action(account, _comment(), "delete")
    comment_ops.run_comment_action(account, _comment(), "pin")

    paths = [item[0] for item in calls]
    assert paths == [
        "/comment/create_comment",
        "/comment/like_comment",
        "/comment/del_comment",
        "/comment/set_top_comment",
    ]
    # 四个接口都在视频号互动域下。
    for _path, _body, api_base in calls:
        assert api_base == comment_ops.ChannelsCommentAdapter.INTERACTION_API_BASE


def test_channels_unlike_and_unpin_use_same_endpoints(monkeypatch):
    comment_ops = pytest.importorskip("comment_ops")
    calls = []

    class FakeClient:
        def _request(self, path, body=None, api_base=None):
            calls.append((path, body or {}))
            return {"errCode": 0}

        def close(self):
            pass

    monkeypatch.setattr(
        comment_ops.ChannelsCommentAdapter, "_client", lambda self: FakeClient(),
    )
    account = {"cookie_path": "", "id": 1}
    comment_ops.run_comment_action(account, _comment(), "unlike")
    comment_ops.run_comment_action(account, _comment(), "unpin")
    assert [item[0] for item in calls] == [
        "/comment/like_comment", "/comment/set_top_comment",
    ]
    # 取消点赞 / 取消置顶必须传反向标记，否则平台会当成"再点一次赞"。
    assert calls[0][1].get("like") == 0
    assert calls[1][1].get("top") == 0


# ---------------------------------------------------------------- 接口层

def _make_ops_db(base: Path) -> None:
    (base / "db").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(base / "db" / "database.db")
    conn.execute(
        "CREATE TABLE user_info ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " type INTEGER, filePath TEXT, userName TEXT, status INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO user_info (type, filePath, userName, status) VALUES (?,?,?,?)",
        [(2, "sph-main.json", "视频号主号", 1), (2, "sph-other.json", "视频号他人号", 1)],
    )
    conn.commit()
    conn.close()


def _make_comment_items(base: Path) -> None:
    import ops_schema

    conn = sqlite3.connect(base / "db" / "ops_center.db")
    conn.row_factory = sqlite3.Row
    ops_schema.ensure_core_schema(conn)
    conn.executemany(
        "INSERT INTO comment_items (platform, account, account_id, comment_id,"
        " source_id, source_title, author, content, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("视频号", "视频号主号", 1, "c-1", "post-1", "作品一", "甲", "多少钱", "2026-11-01 10:00:00"),
            ("视频号", "视频号他人号", 2, "c-2", "post-2", "作品二", "乙", "有优惠吗", "2026-11-01 11:00:00"),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    be = pytest.importorskip("sau_backend")
    _make_ops_db(tmp_path)
    _make_comment_items(tmp_path)
    monkeypatch.setattr(be, "BASE_DIR", tmp_path, raising=True)
    access_scope.bind_base_dir(lambda: be.BASE_DIR)
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", "")
    # 评论操作连的是私信库；测试里换成临时库。
    import dm_ops

    monkeypatch.setattr(
        dm_ops, "get_conn",
        lambda: dm_ops.connect(tmp_path / "db" / "ops_center.db", timeout=10),
        raising=True,
    )
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
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "secret")
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", "")
    monkeypatch.setattr(runtime_config, "authenticate", lambda _token: identity)


def test_comment_list_local_mode_returns_all(client):
    resp = client.get("/comments")
    assert resp.status_code == 200
    items = resp.get_json()["data"]["items"]
    assert len(items) == 2
    # 能力矩阵一起返回，前端据此渲染按钮。
    assert resp.get_json()["data"]["capabilities"]


def test_comment_list_filters_by_scope(client, monkeypatch):
    _use_identity(monkeypatch, _identity("operator", scope=["视频号主号"]))
    resp = client.get("/comments")
    assert resp.status_code == 200
    items = resp.get_json()["data"]["items"]
    assert [item["account"] for item in items] == ["视频号主号"]


def test_out_of_scope_comment_action_is_forbidden(client, monkeypatch):
    _use_identity(monkeypatch, _identity("operator", scope=["视频号主号"]))
    resp = client.post("/comments/2/like", json={})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 403


def test_out_of_scope_comment_is_hidden_from_list(client, monkeypatch):
    _use_identity(monkeypatch, _identity("operator", scope=["视频号主号"]))
    resp = client.get("/comments")
    ids = [item["id"] for item in resp.get_json()["data"]["items"]]
    assert 2 not in ids


def test_viewer_cannot_operate_comments(client, monkeypatch):
    _use_identity(monkeypatch, _identity("viewer", scope=["视频号主号"]))
    resp = client.post("/comments/1/like", json={})
    assert resp.status_code == 403


def test_comment_action_updates_state(client, monkeypatch):
    """操作成功后状态必须回写，否则前端按钮状态会一直停在旧值。"""
    comment_ops = pytest.importorskip("comment_ops")

    class FakeClient:
        def _request(self, path, body=None, api_base=None):
            return {"errCode": 0}

        def close(self):
            pass

    monkeypatch.setattr(
        comment_ops.ChannelsCommentAdapter, "_client", lambda self: FakeClient(),
    )
    resp = client.post("/comments/1/pin", json={})
    assert resp.status_code == 200
    assert resp.get_json()["code"] == 200

    resp = client.get("/comments")
    item = next(
        row for row in resp.get_json()["data"]["items"] if row["id"] == 1
    )
    assert item["pinned"] == 1

    resp = client.post("/comments/1/pin", json={"pinned": False})
    assert resp.status_code == 200
    resp = client.get("/comments")
    item = next(
        row for row in resp.get_json()["data"]["items"] if row["id"] == 1
    )
    assert item["pinned"] == 0


def test_comment_reply_requires_text(client):
    resp = client.post("/comments/1/reply", json={"text": "  "})
    assert resp.status_code == 400


def test_comment_capabilities_endpoint(client):
    resp = client.get("/comments/capabilities")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    names = {item["name"] for item in data}
    assert "视频号" in names and "抖音" in names
