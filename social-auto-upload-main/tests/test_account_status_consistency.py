from __future__ import annotations

from account_store import AccountStore
import publish_jobs


def test_health_snapshot_uses_live_accounts_and_current_status(tmp_path, monkeypatch):
    (tmp_path / "db").mkdir()
    (tmp_path / "cookiesFile").mkdir()
    store = AccountStore(tmp_path)
    online = store.upsert(2, cookie_name="online.json", name="现存正常", status=1)
    expired = store.upsert(3, cookie_name="expired.json", name="现存异常", status=0)

    monkeypatch.setattr(publish_jobs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(publish_jobs, "DB", tmp_path / "db" / "publish_jobs.db")
    publish_jobs.init_db()
    with publish_jobs.get_conn() as conn:
        # 历史巡检说在线，但账号库后来已复检为异常：以账号库为准。
        conn.execute(
            "INSERT INTO account_patrol (account_id, account_name, type, alive) VALUES (?,?,?,?)",
            (expired["id"], "旧巡检名", 3, 1),
        )
        # 已删除账号留下的孤儿记录不能出现在当前状态里。
        conn.execute(
            "INSERT INTO account_patrol (account_id, account_name, type, alive) VALUES (?,?,?,?)",
            (9999, "已删除账号", 1, 0),
        )

    snapshot = publish_jobs.latest_account_status()

    assert [row["account_id"] for row in snapshot] == [online["id"], expired["id"]]
    assert [row["account_name"] for row in snapshot] == ["现存正常", "现存异常"]
    assert [row["alive"] for row in snapshot] == [True, False]


def test_clear_account_patrol_only_removes_target_account(tmp_path, monkeypatch):
    (tmp_path / "db").mkdir()
    monkeypatch.setattr(publish_jobs, "DB", tmp_path / "db" / "publish_jobs.db")
    publish_jobs.init_db()
    with publish_jobs.get_conn() as conn:
        conn.executemany(
            "INSERT INTO account_patrol (account_id, account_name, type, alive) VALUES (?,?,?,?)",
            [(1, "A", 2, 1), (2, "B", 3, 0)],
        )

    removed = publish_jobs.clear_account_patrol(1)

    assert removed == 1
    with publish_jobs.get_conn() as conn:
        rows = conn.execute("SELECT account_id FROM account_patrol ORDER BY account_id").fetchall()
    assert [row["account_id"] for row in rows] == [2]
