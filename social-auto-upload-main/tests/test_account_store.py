# -*- coding: utf-8 -*-
"""账号服务（account_store）回归测试。

锁定账号矩阵式账号模型的四条不变量：

1. **身份唯一**：同一视频号/抖音号重复登录只更新一行，不会每次新增；
2. **身份优先于槽位**：即使传了 ``account_id``，身份命中也会拒绝串号覆盖；
3. **老库可升级**：只有 5 列的 ``user_info`` 能自动补列并回填身份，重复身份
   被标记为 ``duplicate-of:<id>`` 而不是静默合并或删除；
4. **健康度与审计**：``mark_checked`` 写 ``last_check_at``/``fail_reason``，
   授权会话能落库并在重启后查询。

测试全部在临时目录里跑，不碰真实数据库与真实 Cookie 文件。
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

from account_store import (  # noqa: E402
    AccountStore,
    extract_identity,
    file_identity,
    platform_type_of,
)

TENCENT_COOKIE = "a" * 32 + ".json"
DOUYIN_COOKIE = "b" * 32 + ".json"
OTHER_COOKIE = "c" * 32 + ".json"


def _storage_state(*, cookies=None, storage=None) -> dict:
    return {
        "cookies": [{"name": name, "value": value, "domain": "channels.weixin.qq.com"} for name, value in (cookies or [])],
        "origins": [
            {
                "origin": "https://channels.weixin.qq.com",
                "localStorage": [{"name": name, "value": value} for name, value in (storage or [])],
            }
        ],
    }


def _write_cookie(base: Path, name: str, payload: dict) -> Path:
    target = base / "cookiesFile" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


@pytest.fixture()
def base(tmp_path) -> Path:
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def store(base) -> AccountStore:
    return AccountStore(base)


# --------------------------------------------------------------- 身份提取


def test_identity_prefers_storage_then_cookie():
    state = _storage_state(
        cookies=[("wxuin", "2789860741"), ("sessionid", "abcdef")],
        storage=[("finder_username", "v2_0600@finder")],
    )
    identity, source = extract_identity(state, 2)
    assert identity == "v2_0600@finder"
    assert source == "localStorage:finder_username"

    identity, source = extract_identity(_storage_state(cookies=[("wxuin", "2789860741")]), 2)
    assert identity == "2789860741"
    assert source == "cookie:wxuin"


def test_identity_ignores_session_only_cookies():
    """只有 sessionid 时必须放弃——误判会把两个账号合并成一个。"""
    identity, source = extract_identity(_storage_state(cookies=[("sessionid", "abcdef")]), 2)
    assert identity == ""
    assert source == ""


def test_identity_handles_bare_cookie_list_file(base):
    path = base / "cookiesFile" / TENCENT_COOKIE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"name": "wxuin", "value": "123"}]), encoding="utf-8")
    assert file_identity(path, 2) == ("123", "cookie:wxuin")


def test_platform_type_normalization():
    assert platform_type_of("tencent") == 2
    assert platform_type_of("视频号") == 2
    assert platform_type_of("2") == 2
    assert platform_type_of("kuaishou") == 4
    assert platform_type_of("不存在") is None


# --------------------------------------------------------------- 去重写入


def test_upsert_dedupes_by_identity(store, base):
    _write_cookie(base, TENCENT_COOKIE, _storage_state(storage=[("finder_username", "finder-A")]))
    first = store.upsert(
        2, cookie_name=TENCENT_COOKIE, name="频道A", identity="finder-A",
        identity_source="localStorage:finder_username",
    )
    assert first["action"] == "created"

    # 同一个频道重新扫码：新 Cookie 文件、新名字，仍然只更新同一行
    _write_cookie(base, DOUYIN_COOKIE, _storage_state(storage=[("finder_username", "finder-A")]))
    second = store.upsert(
        2, cookie_name=DOUYIN_COOKIE, name="频道A-改名", identity="finder-A",
        identity_source="localStorage:finder_username",
    )
    assert second["action"] == "merged"
    assert second["id"] == first["id"]

    rows = store.list(2)
    assert len(rows) == 1
    assert rows[0]["cookie"]["name"] == DOUYIN_COOKIE
    # 覆盖登录后旧文件不再被引用，应当清理
    assert not (base / "cookiesFile" / TENCENT_COOKIE).exists()
    assert (base / "cookiesFile" / DOUYIN_COOKIE).exists()


def test_upsert_identity_beats_explicit_slot(store, base):
    """手滑选错覆盖目标时，身份命中优先，不能把 A 的凭据写进 B 的记录。"""
    _write_cookie(base, TENCENT_COOKIE, _storage_state(storage=[("finder_username", "finder-A")]))
    a = store.upsert(2, cookie_name=TENCENT_COOKIE, name="A", identity="finder-A")

    _write_cookie(base, DOUYIN_COOKIE, _storage_state(storage=[("finder_username", "finder-B")]))
    b = store.upsert(2, cookie_name=DOUYIN_COOKIE, name="B", identity="finder-B")
    assert b["id"] != a["id"]

    # 用 A 的身份去覆盖 B 的槽位：应更新 A，B 保持原样
    _write_cookie(base, OTHER_COOKIE, _storage_state(storage=[("finder_username", "finder-A")]))
    result = store.upsert(
        2, cookie_name=OTHER_COOKIE, name="A2", account_id=b["id"], identity="finder-A"
    )
    assert result["id"] == a["id"]
    rows = {row["id"]: row for row in store.list(2)}
    assert rows[b["id"]]["cookie"]["name"] == DOUYIN_COOKIE
    assert rows[a["id"]]["cookie"]["name"] == OTHER_COOKIE


def test_upsert_without_identity_uses_slot(store, base):
    _write_cookie(base, TENCENT_COOKIE, _storage_state(cookies=[("sessionid", "x")]))
    created = store.upsert(2, cookie_name=TENCENT_COOKIE, name="无身份账号")
    _write_cookie(base, DOUYIN_COOKIE, _storage_state(cookies=[("sessionid", "y")]))
    updated = store.upsert(
        2, cookie_name=DOUYIN_COOKIE, name="无身份账号", account_id=created["id"]
    )
    assert updated["id"] == created["id"]
    assert updated["action"] == "updated"
    assert len(store.list(2)) == 1


def test_upsert_rejects_unknown_platform(store):
    with pytest.raises(ValueError):
        store.upsert("no-such-platform", cookie_name=TENCENT_COOKIE, name="x")


# --------------------------------------------------------------- 老库升级


def _legacy_rows(base: Path, rows) -> None:
    conn = sqlite3.connect(base / "db" / "database.db")
    conn.execute(
        "CREATE TABLE user_info ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " type INTEGER, filePath TEXT, userName TEXT, status INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO user_info (type, filePath, userName, status) VALUES (?,?,?,?)", rows
    )
    conn.commit()
    conn.close()


def test_legacy_schema_is_migrated_and_backfilled(base):
    _write_cookie(base, TENCENT_COOKIE, _storage_state(storage=[("finder_username", "finder-A")]))
    _legacy_rows(base, [(2, TENCENT_COOKIE, "老账号", 1)])

    store = AccountStore(base)
    assert store.ensure_schema() is True
    rows = store.list(2)
    assert len(rows) == 1
    assert rows[0]["identity"]["value"] == "finder-A"
    assert rows[0]["identity"]["source"] == "localStorage:finder_username"
    assert rows[0]["status"] == 1

    # 老库没有 created_at/updated_at，迁移后要补上，便于排序与排障
    assert rows[0]["updatedAt"]


def test_legacy_duplicate_identity_is_marked_not_deleted(base):
    _write_cookie(base, TENCENT_COOKIE, _storage_state(storage=[("finder_username", "finder-A")]))
    _write_cookie(base, DOUYIN_COOKIE, _storage_state(storage=[("finder_username", "finder-A")]))
    _legacy_rows(
        base,
        [
            (2, TENCENT_COOKIE, "频道A-旧", 1),
            (2, DOUYIN_COOKIE, "频道A-重复", 1),
        ],
    )
    store = AccountStore(base)
    store.ensure_schema()

    rows = store.list(2)
    assert len(rows) == 2  # 绝不删用户数据
    bound = [row for row in rows if row["identity"]["bound"]]
    duplicates = [row for row in rows if not row["identity"]["bound"]]
    assert len(bound) == 1
    assert len(duplicates) == 1
    assert duplicates[0]["status"] == 0
    assert duplicates[0]["failReason"].startswith("duplicate-of:")


# --------------------------------------------------------------- 健康度/审计


def test_mark_checked_records_reason_and_timestamp(store, base):
    _write_cookie(base, TENCENT_COOKIE, _storage_state(storage=[("finder_username", "finder-A")]))
    account = store.upsert(2, cookie_name=TENCENT_COOKIE, name="A", identity="finder-A")
    # 连续 2 次失败才判异常：第一次只记录"未通过（1/2）"，状态保持
    assert store.mark_checked(account["id"], False, "登录态失效，请重新授权") is True
    row = store.get(account["id"])
    assert row["status"] == 1
    assert row["failStreak"] == 1
    assert "1/2" in row["failReason"]
    assert row["lastCheckAt"]

    # 第二次失败才拉红
    store.mark_checked(account["id"], False, "登录态失效，请重新授权")
    row = store.get(account["id"])
    assert row["status"] == 0
    assert row["failStreak"] == 2
    assert row["failReason"] == "登录态失效，请重新授权"

    # 一次成功立刻清零并恢复
    store.mark_checked(account["id"], True)
    row = store.get(account["id"])
    assert row["status"] == 1
    assert row["failStreak"] == 0
    assert row["failReason"] == ""


def test_mark_checked_threshold_is_configurable(store, base):
    _write_cookie(base, TENCENT_COOKIE, _storage_state(storage=[("finder_username", "finder-A")]))
    account = store.upsert(2, cookie_name=TENCENT_COOKIE, name="A", identity="finder-A")
    store.mark_checked(account["id"], False, "x", threshold=1)
    assert store.get(account["id"])["status"] == 0


def test_same_session_is_grouped(store, base):
    """同一次扫码保存的多个频道共享 sessionid = 同一个会话组。

    前端用这个标记提示"共用登录"：它们会一起失效，也会一起修好。
    两个独立登录的账号则必须落在不同组里。
    """
    shared = _storage_state(
        cookies=[("sessionid", "same-session-value")],
        storage=[("finder_username", "finder-A")],
    )
    _write_cookie(base, TENCENT_COOKIE, shared)
    store.upsert(2, cookie_name=TENCENT_COOKIE, name="A", identity="finder-A")

    shared_b = _storage_state(
        cookies=[("sessionid", "same-session-value")],
        storage=[("finder_username", "finder-B")],
    )
    _write_cookie(base, DOUYIN_COOKIE, shared_b)
    store.upsert(2, cookie_name=DOUYIN_COOKIE, name="B", identity="finder-B")

    alone = _storage_state(
        cookies=[("sessionid", "another-session")],
        storage=[("finder_username", "finder-C")],
    )
    _write_cookie(base, OTHER_COOKIE, alone)
    store.upsert(2, cookie_name=OTHER_COOKIE, name="C", identity="finder-C")

    rows = {row["name"]: row for row in store.list(2)}
    group_a = rows["A"]["cookie"]["sessionGroup"]
    group_b = rows["B"]["cookie"]["sessionGroup"]
    group_c = rows["C"]["cookie"]["sessionGroup"]
    assert group_a and group_a == group_b
    assert group_c and group_c != group_a


def test_stats_and_orphans(store, base):
    _write_cookie(base, TENCENT_COOKIE, _storage_state(storage=[("finder_username", "finder-A")]))
    _write_cookie(base, DOUYIN_COOKIE, _storage_state(storage=[("finder_username", "finder-B")]))
    _write_cookie(base, OTHER_COOKIE, _storage_state(storage=[("finder_username", "finder-C")]))
    store.upsert(2, cookie_name=TENCENT_COOKIE, name="A", identity="finder-A")
    store.upsert(2, cookie_name=DOUYIN_COOKIE, name="B", identity="finder-B")

    stats = store.stats()
    assert stats["total"] == 2
    assert stats["byPlatform"]["视频号"] == 2
    assert stats["orphanCookies"] == 1

    dry = store.prune_orphans(dry_run=True)
    assert dry["count"] == 1 and dry["files"] == [OTHER_COOKIE]
    assert (base / "cookiesFile" / OTHER_COOKIE).exists()

    real = store.prune_orphans(dry_run=False)
    assert real["removed"] == [OTHER_COOKIE]
    assert not (base / "cookiesFile" / OTHER_COOKIE).exists()


def test_auth_session_audit_roundtrip(store):
    store.record_auth_session(
        "sess-1", 2, account_name="频道A", account_id=None, mode="create", actor="local"
    )
    running = store.list_auth_sessions()
    assert running[0]["id"] == "sess-1"
    assert running[0]["state"] == "running"
    assert running[0]["platform"]["label"] == "视频号"

    store.finish_auth_session("sess-1", "success", "登录成功", TENCENT_COOKIE)
    done = store.list_auth_sessions()[0]
    assert done["state"] == "success"
    assert done["cookie_name"] == TENCENT_COOKIE
    assert done["finished_at"]


# --------------------------------------------------------------- HTTP 接口


@pytest.fixture()
def client(base, monkeypatch):
    be = pytest.importorskip("sau_backend")
    import runtime_config

    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    monkeypatch.setattr(be, "BASE_DIR", base, raising=True)
    _write_cookie(
        base,
        TENCENT_COOKIE,
        _storage_state(cookies=[("wxuin", "2789860741")], storage=[("finder_username", "finder-A")]),
    )
    return be, be.app.test_client()


def test_api_v2_accounts_shape(client):
    be, http = client
    store = be.get_store(be.BASE_DIR)
    account = store.upsert(
        2, cookie_name=TENCENT_COOKIE, name="频道A", identity="finder-A",
        identity_source="localStorage:finder_username",
    )

    payload = http.get("/api/v2/accounts").get_json()["data"]
    assert payload["platforms"][1]["key"] == "tencent"
    account_row = payload["accounts"][0]
    assert account_row["id"] == account["id"]
    assert account_row["platform"]["label"] == "视频号"
    assert account_row["identity"]["masked"].startswith("fin")
    assert account_row["cookie"]["exists"] is True
    assert payload["stats"]["total"] == 1

    detail = http.get(f"/api/v2/accounts/{account['id']}").get_json()["data"]
    assert detail["name"] == "频道A"

    creds = http.get(f"/api/v2/accounts/{account['id']}/credentials").get_json()["data"]
    assert "finder_username" in creds["storageKeys"]
    assert "wxuin" in creds["cookieNames"]
    assert creds["domains"] == ["channels.weixin.qq.com"]
    # 绝不能把 Cookie 值吐出来
    serialized = json.dumps(creds, ensure_ascii=False)
    assert "finder-A" not in serialized
    assert "2789860741" not in serialized


def test_api_v2_legacy_endpoint_still_five_columns(client):
    be, http = client
    be.get_store(be.BASE_DIR).upsert(
        2, cookie_name=TENCENT_COOKIE, name="频道A", identity="finder-A"
    )
    data = http.get("/getAccounts").get_json()["data"]
    assert len(data) == 1
    assert len(data[0]) == 5  # 前端按索引取 0..4，多一列都会让旧映射错位
    assert data[0][3] == "频道A"


def test_api_v2_prune_and_backfill(client):
    be, http = client
    be.get_store(be.BASE_DIR).upsert(
        2, cookie_name=TENCENT_COOKIE, name="频道A", identity="finder-A"
    )
    _write_cookie(be.BASE_DIR, OTHER_COOKIE, _storage_state(storage=[("finder_username", "finder-Z")]))

    dry = http.post("/api/v2/accounts/prune-cookies").get_json()["data"]
    assert dry["dryRun"] is True and dry["count"] == 1

    real = http.post("/api/v2/accounts/prune-cookies?dry=0").get_json()["data"]
    assert real["removed"] == [OTHER_COOKIE]

    backfill = http.post("/api/v2/accounts/backfill-identities").get_json()["data"]
    assert set(backfill) == {"filled", "conflicts"}


def test_api_v2_publish_transport_reports_and_clears_templates(client, monkeypatch):
    """发布通道接口：报出通道模式和模板状态，并能清掉模板。"""
    be, http = client
    monkeypatch.setenv("FLOWOPS_PUBLISH_TRANSPORT", "hybrid")
    store = be.get_store(be.BASE_DIR)
    account = store.upsert(
        3, cookie_name=DOUYIN_COOKIE, name="抖音号", identity="douyin-A",
    )
    cookie_path = be.BASE_DIR / "cookiesFile" / DOUYIN_COOKIE

    from publish_transport import save_template

    save_template(cookie_path, 3, {
        "url": "https://creator.douyin.com/web/api/media/aweme/create_v2/?aid=1128",
        "method": "POST",
        "headers": {},
        "body": {"vid": "{{media:vid}}"},
        "signature": "sig",
    })

    payload = http.get(
        f"/api/v2/accounts/{account['id']}/publish-transport"
    ).get_json()["data"]
    douyin_row = next(row for row in payload if row["platformType"] == 3)
    assert douyin_row["hybridEnabled"] is True
    assert douyin_row["templateReady"] is True

    cleared = http.delete(
        f"/api/v2/accounts/{account['id']}/publish-transport"
    ).get_json()["data"]
    assert cleared["cleared"] is True

    payload = http.get(
        f"/api/v2/accounts/{account['id']}/publish-transport"
    ).get_json()["data"]
    douyin_row = next(row for row in payload if row["platformType"] == 3)
    assert douyin_row["templateReady"] is False
