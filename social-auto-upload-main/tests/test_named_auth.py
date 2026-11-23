from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import access_scope
import runtime_config
import user_auth
from utils import credential_crypto
from utils.request_limiter import reset_for_tests


def _make_main_db(base: Path) -> Path:
    db = base / "db" / "database.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE user_info ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER, filePath TEXT, "
            "userName TEXT, status INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO user_info (type,filePath,userName,status) VALUES (3,?,?,1)",
            ("dy-main.json", "抖音主号"),
        )
    return db


@pytest.fixture()
def auth_env(tmp_path, monkeypatch):
    be = pytest.importorskip("sau_backend")
    import conf
    import rbac_audit

    main_db = _make_main_db(tmp_path)
    monkeypatch.setattr(be, "BASE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(conf, "BASE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(user_auth, "DB", main_db, raising=True)
    monkeypatch.setattr(rbac_audit, "DB", main_db, raising=True)
    access_scope.bind_base_dir(lambda: be.BASE_DIR)
    rbac_audit.init_rbac()
    user_auth.init_auth_schema(force=True)

    monkeypatch.setattr(runtime_config, "PERSONAL_AUTH", True, raising=True)
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "bootstrap-token", raising=True)
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", "", raising=True)
    monkeypatch.setattr(user_auth, "PASSWORD_ITERATIONS", 2_000, raising=True)
    monkeypatch.setenv(credential_crypto.MASTER_KEY_ENV, "named-auth-test-secret")
    credential_crypto.reset_master_key_cache()
    reset_for_tests()
    admin = user_auth.create_user(
        "admin01", "AdminPass-2026!", "admin", [],
        display_name="管理员甲", must_change_password=False,
    )
    return SimpleNamespace(
        client=be.app.test_client(),
        db=main_db,
        admin=admin,
        user_auth=user_auth,
    )


def _login(client, username: str, password: str):
    response = client.post(
        "/api/v2/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _headers(token: str) -> dict[str, str]:
    return {"X-YunCe-Token": token}


def test_legacy_auth_schema_is_upgraded_in_place(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "username TEXT UNIQUE NOT NULL,password_hash TEXT DEFAULT '',"
            "role TEXT DEFAULT 'operator',scope TEXT DEFAULT '',"
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY,user_id INTEGER,"
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP,expires_at DATETIME)"
        )
        conn.execute(
            "INSERT INTO users (username,password_hash,role,scope) "
            "VALUES ('legacy','7f3a6aae7a5d2d9ddc93ec4d5ea6b874dd9c4bf71cb150ae6b90357aef4d3d12',"
            "'admin','[]')"
        )
    monkeypatch.setattr(user_auth, "DB", db, raising=True)
    user_auth.init_auth_schema(force=True)
    with sqlite3.connect(db) as conn:
        user_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        session_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"display_name", "disabled", "mfa_secret", "mfa_enabled", "mfa_last_counter"} <= user_columns
    assert {"last_seen_at", "revoked_at", "ip", "user_agent"} <= session_columns
    assert {"auth_login_failures", "mfa_challenges"} <= tables


def test_password_hash_is_slow_and_verifiable(auth_env, monkeypatch):
    monkeypatch.setattr(user_auth, "PASSWORD_ITERATIONS", 2_000, raising=True)
    encoded = user_auth._hash_password("correct horse battery")
    assert encoded.startswith("pbkdf2_sha256$2000$")
    assert user_auth.verify_password("correct horse battery", encoded)
    assert not user_auth.verify_password("wrong password", encoded)


def test_login_returns_session_but_database_stores_only_hash(auth_env):
    result = _login(auth_env.client, "admin01", "AdminPass-2026!")
    token = result["token"]
    assert len(token) >= 40
    with sqlite3.connect(auth_env.db) as conn:
        stored = {
            row[0] for row in conn.execute("SELECT id FROM sessions").fetchall()
        }
    assert user_auth.hash_session_token(token) in stored
    assert token not in stored

    session = auth_env.client.get("/api/v2/session", headers=_headers(token))
    assert session.status_code == 200
    data = session.get_json()["data"]
    assert data["user"] == "admin01"
    assert data["displayName"] == "管理员甲"
    assert data["role"] == "admin"
    assert data["source"] == "session"


def test_invalid_and_disabled_users_fail_closed(auth_env):
    bad = auth_env.client.post(
        "/api/v2/auth/login",
        json={"username": "admin01", "password": "wrong-password"},
    )
    assert bad.status_code == 401
    assert bad.get_json()["msg"] == "用户名或密码不正确"

    auth_env.user_auth.create_user(
        "admin02", "SecondAdmin-2026!", "admin", [],
        display_name="管理员乙", must_change_password=False,
    )
    login = _login(auth_env.client, "admin01", "AdminPass-2026!")
    auth_env.user_auth.update_user(auth_env.admin["id"], disabled=True)
    denied = auth_env.client.get("/api/v2/session", headers=_headers(login["token"]))
    assert denied.status_code == 401


def test_logout_and_password_change_revoke_sessions(auth_env):
    first = _login(auth_env.client, "admin01", "AdminPass-2026!")
    logout = auth_env.client.post("/api/v2/auth/logout", headers=_headers(first["token"]))
    assert logout.status_code == 200
    assert logout.get_json()["data"]["revoked"] is True
    assert auth_env.client.get(
        "/api/v2/session", headers=_headers(first["token"])
    ).status_code == 401

    second = _login(auth_env.client, "admin01", "AdminPass-2026!")
    changed = auth_env.client.post(
        "/api/v2/auth/password",
        headers=_headers(second["token"]),
        json={
            "currentPassword": "AdminPass-2026!",
            "newPassword": "NewAdminPass-2026!",
        },
    )
    assert changed.status_code == 200
    assert auth_env.client.get(
        "/api/v2/session", headers=_headers(second["token"])
    ).status_code == 401
    _login(auth_env.client, "admin01", "NewAdminPass-2026!")


def test_admin_manages_users_but_operator_cannot(auth_env):
    admin_login = _login(auth_env.client, "admin01", "AdminPass-2026!")
    created = auth_env.client.post(
        "/api/v2/users",
        headers=_headers(admin_login["token"]),
        json={
            "username": "operator01",
            "displayName": "运营甲",
            "password": "OperatorPass-2026!",
            "role": "operator",
            "scope": ["dy-main.json"],
            "mustChangePassword": False,
        },
    )
    assert created.status_code == 200, created.get_json()
    operator_id = created.get_json()["data"]["id"]

    operator_login = _login(auth_env.client, "operator01", "OperatorPass-2026!")
    denied = auth_env.client.get("/api/v2/users", headers=_headers(operator_login["token"]))
    assert denied.status_code == 403

    disabled = auth_env.client.patch(
        f"/api/v2/users/{operator_id}",
        headers=_headers(admin_login["token"]),
        json={"disabled": True},
    )
    assert disabled.status_code == 200
    assert disabled.get_json()["data"]["disabled"] is True
    assert auth_env.client.get(
        "/api/v2/session", headers=_headers(operator_login["token"])
    ).status_code == 401


def test_last_active_admin_cannot_be_disabled(auth_env):
    login = _login(auth_env.client, "admin01", "AdminPass-2026!")
    response = auth_env.client.patch(
        f"/api/v2/users/{auth_env.admin['id']}",
        headers=_headers(login["token"]),
        json={"disabled": True},
    )
    assert response.status_code == 400
    assert "最后一个有效管理员" in response.get_json()["msg"]
    assert auth_env.client.get(
        "/api/v2/session", headers=_headers(login["token"])
    ).status_code == 200


def test_auth_config_is_public_and_reports_limits(auth_env):
    response = auth_env.client.get("/api/v2/auth/config")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["personalAuthEnabled"] is True
    assert data["passwordMinLength"] == 12
    assert data["tokenFallbackEnabled"] is True
    assert data["mfaAvailable"] is True
    assert "activeAdmins" not in data


def test_login_rate_limit_is_applied_per_username(auth_env):
    for _ in range(10):
        response = auth_env.client.post(
            "/api/v2/auth/login",
            json={"username": "admin01", "password": "wrong-password"},
        )
        assert response.status_code == 401
    blocked = auth_env.client.post(
        "/api/v2/auth/login",
        json={"username": "admin01", "password": "AdminPass-2026!"},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_totp_setup_login_and_replay_protection(auth_env):
    login = _login(auth_env.client, "admin01", "AdminPass-2026!")
    headers = _headers(login["token"])
    setup = auth_env.client.post(
        "/api/v2/auth/mfa/setup",
        headers=headers,
        json={"currentPassword": "AdminPass-2026!"},
    )
    assert setup.status_code == 200, setup.get_json()
    setup_data = setup.get_json()["data"]
    assert setup_data["secret"]
    assert setup_data["qrDataUrl"].startswith("data:image/png;base64,")
    counter = int(__import__("time").time()) // user_auth.TOTP_INTERVAL
    code = user_auth._totp_code(setup_data["secret"], counter)
    confirmed = auth_env.client.post(
        "/api/v2/auth/mfa/confirm",
        headers=headers,
        json={"code": code},
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    assert confirmed.get_json()["data"]["mfaEnabled"] is True
    with sqlite3.connect(auth_env.db) as conn:
        encrypted_secret = conn.execute(
            "SELECT mfa_secret FROM users WHERE username='admin01'"
        ).fetchone()[0]
    assert encrypted_secret
    assert setup_data["secret"] not in encrypted_secret

    auth_env.client.post("/api/v2/auth/logout", headers=headers)
    with sqlite3.connect(auth_env.db) as conn:
        conn.execute("UPDATE users SET mfa_last_counter=0 WHERE username='admin01'")
    first_factor = _login(auth_env.client, "admin01", "AdminPass-2026!")
    assert first_factor["mfaRequired"] is True
    code = user_auth._totp_code(setup_data["secret"], counter)
    verified = auth_env.client.post(
        "/api/v2/auth/mfa/verify",
        json={"challengeToken": first_factor["challengeToken"], "code": code},
    )
    assert verified.status_code == 200, verified.get_json()
    token = verified.get_json()["data"]["token"]
    assert auth_env.client.get("/api/v2/session", headers=_headers(token)).status_code == 200

    replay_challenge = auth_env.client.post(
        "/api/v2/auth/login",
        json={"username": "admin01", "password": "AdminPass-2026!"},
    ).get_json()["data"]["challengeToken"]
    replay = auth_env.client.post(
        "/api/v2/auth/mfa/verify",
        json={"challengeToken": replay_challenge, "code": code},
    )
    assert replay.status_code == 401

    with sqlite3.connect(auth_env.db) as conn:
        conn.execute("UPDATE users SET mfa_last_counter=0 WHERE username='admin01'")
    disabled = auth_env.client.post(
        "/api/v2/auth/mfa/disable",
        headers=_headers(token),
        json={"currentPassword": "AdminPass-2026!", "code": code},
    )
    assert disabled.status_code == 200, disabled.get_json()
    assert disabled.get_json()["data"]["mfaEnabled"] is False


def test_persistent_lockout_and_admin_unlock(auth_env, monkeypatch):
    monkeypatch.setenv("FLOWOPS_LOGIN_FAILURE_LIMIT", "3")
    for _ in range(3):
        response = auth_env.client.post(
            "/api/v2/auth/login",
            json={"username": "admin01", "password": "wrong-password"},
        )
        assert response.status_code == 401
    locked = auth_env.client.post(
        "/api/v2/auth/login",
        json={"username": "admin01", "password": "AdminPass-2026!"},
    )
    assert locked.status_code == 429
    with sqlite3.connect(auth_env.db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM auth_login_failures WHERE locked_until>datetime('now')"
        ).fetchone()[0] >= 1

    auth_env.user_auth.create_user(
        "admin02", "SecondAdmin-2026!", "admin", [],
        display_name="管理员乙", must_change_password=False,
    )
    unlocker = _login(auth_env.client, "admin02", "SecondAdmin-2026!")
    unlocked = auth_env.client.post(
        f"/api/v2/users/{auth_env.admin['id']}/unlock-login",
        headers=_headers(unlocker["token"]),
    )
    assert unlocked.status_code == 200
    _login(auth_env.client, "admin01", "AdminPass-2026!")


def test_admin_can_reset_mfa_and_revoke_sessions(auth_env):
    victim_login = _login(auth_env.client, "admin01", "AdminPass-2026!")
    setup = auth_env.client.post(
        "/api/v2/auth/mfa/setup",
        headers=_headers(victim_login["token"]),
        json={"currentPassword": "AdminPass-2026!"},
    ).get_json()["data"]
    counter = int(__import__("time").time()) // user_auth.TOTP_INTERVAL
    code = user_auth._totp_code(setup["secret"], counter)
    assert auth_env.client.post(
        "/api/v2/auth/mfa/confirm",
        headers=_headers(victim_login["token"]),
        json={"code": code},
    ).status_code == 200

    auth_env.user_auth.create_user(
        "admin02", "SecondAdmin-2026!", "admin", [],
        display_name="管理员乙", must_change_password=False,
    )
    admin_login = _login(auth_env.client, "admin02", "SecondAdmin-2026!")
    reset = auth_env.client.post(
        f"/api/v2/users/{auth_env.admin['id']}/mfa-reset",
        headers=_headers(admin_login["token"]),
    )
    assert reset.status_code == 200
    assert reset.get_json()["data"]["mfaEnabled"] is False
    assert auth_env.client.get(
        "/api/v2/session", headers=_headers(victim_login["token"])
    ).status_code == 401


def test_metrics_are_available_to_admin_only(auth_env):
    admin_login = _login(auth_env.client, "admin01", "AdminPass-2026!")
    metrics = auth_env.client.get("/api/metrics", headers=_headers(admin_login["token"]))
    assert metrics.status_code == 200
    body = metrics.get_data(as_text=True)
    assert "yunce_auth_users_total 1" in body
    assert "yunce_http_requests_total" in body

    auth_env.user_auth.create_user(
        "operator01", "OperatorPass-2026!", "operator", ["dy-main.json"],
        display_name="运营甲", must_change_password=False,
    )
    operator_login = _login(auth_env.client, "operator01", "OperatorPass-2026!")
    denied = auth_env.client.get("/api/metrics", headers=_headers(operator_login["token"]))
    assert denied.status_code == 403
    status_denied = auth_env.client.get(
        "/api/v2/auth/status", headers=_headers(operator_login["token"])
    )
    assert status_denied.status_code == 403
    status_ok = auth_env.client.get(
        "/api/v2/auth/status", headers=_headers(admin_login["token"])
    )
    assert status_ok.status_code == 200
