# -*- coding: utf-8 -*-
"""Named users, password login and revocable server-side sessions.

The legacy global token and hashed user-token file remain supported for local
use and bootstrap. Production teams should use per-user sessions so that role
changes, password changes and employee offboarding take effect on the next
request instead of requiring a shared secret to be rotated everywhere.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import Blueprint, g, jsonify, request
from loguru import logger

from conf import BASE_DIR
from db_utils import connect, record_schema_version
import runtime_config


DB = Path(BASE_DIR / "db" / "database.db")
bp = Blueprint("named_user_auth", __name__)

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 256
PASSWORD_ITERATIONS = 600_000
def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return int(default)
    return max(1, value)


SESSION_TTL_HOURS = _env_positive_int("FLOWOPS_SESSION_TTL_HOURS", 12)
SESSION_TOUCH_SECONDS = 300
MAX_SESSIONS_PER_USER = 10
#: 会话滑动：剩余不足一半时自动续到完整窗口，最多滑到绝对上限
SESSION_SLIDING_ENABLED = True
SESSION_SLIDE_RATIO = 0.5
SESSION_ABSOLUTE_DAYS = _env_positive_int("FLOWOPS_SESSION_ABSOLUTE_DAYS", 90)
#: 刷新令牌（默认 30 天，旋转 + 复用检测）
REFRESH_TTL_DAYS = _env_positive_int("FLOWOPS_REFRESH_TTL_DAYS", 30)
REFRESH_COOKIE_NAME = "yunce_refresh"
REFRESH_COOKIE_PATH = "/api/v2/auth"
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{2,63}$")

TOTP_INTERVAL = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1
MFA_CHALLENGE_TTL_SECONDS = 300
MFA_MAX_ATTEMPTS = 5
LOGIN_FAILURE_LIMIT = 10
LOGIN_IP_FAILURE_LIMIT = 60
LOGIN_FAILURE_WINDOW_SECONDS = 900
LOGIN_LOCK_SECONDS = 900

_SCHEMA_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()


class AuthError(ValueError):
    """A user-facing validation or authentication failure."""


class LoginLockedError(AuthError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"登录失败次数过多，请在 {self.retry_after} 秒后重试")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).strftime("%Y-%m-%d %H:%M:%S")


def _parse_stamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _ensure_column(conn, table: str, column: str, definition: str) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_auth_schema(*, force: bool = False) -> None:
    """Create and upgrade auth tables once for each database path."""
    path = str(Path(DB))
    with _SCHEMA_LOCK:
        if path in _INITIALIZED and not force:
            return
        DB.parent.mkdir(parents=True, exist_ok=True)
        with connect(DB, timeout=30) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'operator',
                    scope TEXT NOT NULL DEFAULT '[]',
                    disabled INTEGER NOT NULL DEFAULT 0,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    password_changed_at DATETIME,
                    last_login_at DATETIME
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME NOT NULL,
                    revoked_at DATETIME,
                    ip TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT ''
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS auth_login_failures (
                    key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    first_failed_at DATETIME,
                    last_failed_at DATETIME,
                    locked_until DATETIME,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS mfa_challenges (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    expires_at DATETIME NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    used_at DATETIME,
                    ip TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT ''
                )"""
            )
            # 刷新令牌（滑动会话）：access 只活 12 小时，refresh 活 30 天。
            # 关掉浏览器/后端重启之后，前端靠它静默换一张新的 access，
            # 用户就不用每天重新登录（对齐"网页端官方登录能一直挂着"的体验）。
            conn.execute(
                """CREATE TABLE IF NOT EXISTS session_refresh_tokens (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    created_at DATETIME NOT NULL,
                    expires_at DATETIME NOT NULL,
                    rotated_at DATETIME,
                    revoked_at DATETIME,
                    ip TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT ''
                )"""
            )
            _ensure_column(conn, "users", "display_name", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "users", "disabled", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(
                conn, "users", "must_change_password",
                "INTEGER NOT NULL DEFAULT 0",
            )
            _ensure_column(
                conn, "users", "updated_at",
                "DATETIME",
            )
            _ensure_column(conn, "users", "password_changed_at", "DATETIME")
            _ensure_column(conn, "users", "last_login_at", "DATETIME")
            _ensure_column(conn, "users", "mfa_secret", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(
                conn, "users", "mfa_pending_secret",
                "TEXT NOT NULL DEFAULT ''",
            )
            _ensure_column(conn, "users", "mfa_enabled", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "users", "mfa_confirmed_at", "DATETIME")
            _ensure_column(
                conn, "users", "mfa_last_counter",
                "INTEGER NOT NULL DEFAULT 0",
            )
            _ensure_column(conn, "sessions", "last_seen_at", "DATETIME")
            _ensure_column(conn, "sessions", "revoked_at", "DATETIME")
            _ensure_column(conn, "sessions", "ip", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(
                conn, "sessions", "user_agent",
                "TEXT NOT NULL DEFAULT ''",
            )
            conn.execute(
                "UPDATE users SET updated_at=COALESCE(updated_at,created_at,CURRENT_TIMESTAMP)"
            )
            conn.execute(
                "UPDATE sessions SET last_seen_at=COALESCE(last_seen_at,created_at,CURRENT_TIMESTAMP)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_user_expiry "
                "ON sessions(user_id, expires_at, revoked_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_role_disabled "
                "ON users(role, disabled)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mfa_challenges_expiry "
                "ON mfa_challenges(user_id, expires_at, used_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_login_failures_locked "
                "ON auth_login_failures(locked_until)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user "
                "ON session_refresh_tokens(user_id, expires_at, revoked_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_session "
                "ON session_refresh_tokens(session_id)"
            )
            record_schema_version(conn, "named_auth", 3)
        _INITIALIZED.add(path)


def _hash_password(
    raw: str, *, iterations: int | None = None, allow_short: bool = False,
) -> str:
    password = str(raw or "")
    if allow_short:
        # 单密码门禁允许使用者自定义短密码（本地自用场景），但空密码仍然拒绝
        if not password or len(password) > PASSWORD_MAX_LENGTH:
            raise AuthError("密码不能为空")
    elif not (PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH):
        raise AuthError(f"密码长度必须为 {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} 个字符")
    rounds = int(PASSWORD_ITERATIONS if iterations is None else iterations)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, rounds
    )
    return "$".join((
        PASSWORD_SCHEME,
        str(rounds),
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    ))


def _legacy_password(raw: str) -> str:
    # Kept only to migrate accounts created by the old placeholder helper.
    return hashlib.sha256(f"yunce:{raw}".encode("utf-8")).hexdigest()


def verify_password(raw: str, encoded: str) -> bool:
    value = str(encoded or "")
    if value.startswith(PASSWORD_SCHEME + "$"):
        try:
            _, iterations, salt_b64, digest_b64 = value.split("$", 3)
            salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
            actual = hashlib.pbkdf2_hmac(
                "sha256", str(raw or "").encode("utf-8"), salt, int(iterations)
            )
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError):
            return False
    if len(value) == 64:
        return hmac.compare_digest(_legacy_password(raw), value)
    return False


def hash_session_token(raw: str) -> str:
    return hashlib.sha256(str(raw or "").encode("utf-8")).hexdigest()


def _scope_value(scope: Any) -> list[str]:
    if isinstance(scope, str):
        text = scope.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                scope = json.loads(text)
            except ValueError:
                scope = text.split(",")
        else:
            scope = text.split(",")
    if not isinstance(scope, (list, tuple, set)):
        raise AuthError("scope 必须是账号标识数组")
    return [str(value).strip() for value in scope if str(value).strip()]


def _scope_json(scope: Any) -> str:
    return json.dumps(_scope_value(scope), ensure_ascii=False)


def _validate_role(role: str) -> str:
    value = str(role or "").strip().lower()
    if value not in runtime_config.PRODUCTION_ROLES:
        raise AuthError("角色必须为 cutter、reviewer、operator 或 admin")
    return value


def _normalize_username(username: str) -> str:
    value = str(username or "").strip()
    if not USERNAME_RE.fullmatch(value):
        raise AuthError("用户名需为 3-64 位字母、数字或 . _ @ -，并以字母或数字开头")
    return value


def _user_public(row: Any) -> dict:
    item = dict(row)
    return {
        "id": item.get("id"),
        "username": item.get("username") or "",
        "displayName": item.get("display_name") or "",
        "role": item.get("role") or "",
        "scope": _scope_value(item.get("scope") or []),
        "disabled": bool(item.get("disabled")),
        "mustChangePassword": bool(item.get("must_change_password")),
        "mfaEnabled": bool(item.get("mfa_enabled")),
        "mfaConfirmedAt": item.get("mfa_confirmed_at"),
        "createdAt": item.get("created_at"),
        "updatedAt": item.get("updated_at"),
        "passwordChangedAt": item.get("password_changed_at"),
        "lastLoginAt": item.get("last_login_at"),
    }


def _identity_for_user(row: Any) -> dict:
    item = dict(row)
    role = str(item.get("role") or "")
    permissions = runtime_config.ROLE_PERMISSIONS.get(role)
    if permissions is None:
        return {}
    return {
        "user_id": int(item.get("id") or 0),
        "user": str(item.get("username") or ""),
        "display_name": str(item.get("display_name") or ""),
        "role": role,
        "scope": _scope_value(item.get("scope") or []),
        "scope_mode": "all" if role == runtime_config.ROLE_ADMIN else "list",
        "permissions": list(permissions),
        "source": "session",
        "must_change_password": bool(item.get("must_change_password")),
        "mfa_enabled": bool(item.get("mfa_enabled")),
        "session_id": str(item.get("session_id") or ""),
    }


def _active_admin_count(conn, *, excluding: int | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0"
    params: tuple[Any, ...] = ()
    if excluding is not None:
        sql += " AND id<>?"
        params = (int(excluding),)
    return int(conn.execute(sql, params).fetchone()["n"] or 0)


def _audit(action: str, target: str = "", detail: str = "", *, actor: str = "") -> None:
    try:
        import rbac_audit

        rbac_audit.record_audit(action, target, detail, actor=actor)
    except Exception:
        pass


def _request_ip() -> str:
    from utils.request_limiter import client_ip

    return str(client_ip() or "unknown")[:128]


def _user_agent() -> str:
    return str(request.headers.get("User-Agent") or "")[:512]


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return max(1, value)


def _failure_keys(username: str, ip: str) -> tuple[tuple[str, str], tuple[str, str]]:
    return (
        ("user:" + str(username or "").casefold()[:64], "user"),
        ("ip:" + str(ip or "unknown")[:128], "ip"),
    )


def _limit_for_category(category: str) -> int:
    if category == "ip":
        return _env_int("FLOWOPS_LOGIN_IP_FAILURE_LIMIT", LOGIN_IP_FAILURE_LIMIT)
    return _env_int("FLOWOPS_LOGIN_FAILURE_LIMIT", LOGIN_FAILURE_LIMIT)


def check_login_lock(username: str, ip: str, conn=None) -> None:
    """Raise when persistent username or IP lockout is still active."""
    own_conn = conn is None
    if own_conn:
        init_auth_schema()
        conn = connect(DB, timeout=15)
    now = _now()
    try:
        waits: list[int] = []
        for key, _category in _failure_keys(username, ip):
            row = conn.execute(
                "SELECT locked_until FROM auth_login_failures WHERE key=?", (key,)
            ).fetchone()
            locked_until = _parse_stamp(row["locked_until"] if row else None)
            if locked_until and locked_until > now:
                waits.append(max(1, int((locked_until - now).total_seconds())))
        if waits:
            raise LoginLockedError(max(waits))
    finally:
        if own_conn:
            conn.close()


def _record_login_failure(username: str, ip: str, *, conn=None) -> None:
    init_auth_schema()
    now = _now()
    now_text = _stamp(now)
    window = _env_int("FLOWOPS_LOGIN_FAILURE_WINDOW", LOGIN_FAILURE_WINDOW_SECONDS)
    lock_seconds = _env_int("FLOWOPS_LOGIN_LOCK_SECONDS", LOGIN_LOCK_SECONDS)
    own_conn = conn is None
    if own_conn:
        conn = connect(DB, timeout=30)
    try:
        stale_before = _stamp(now - timedelta(days=7))
        conn.execute(
            "DELETE FROM auth_login_failures WHERE updated_at<?", (stale_before,)
        )
        for key, category in _failure_keys(username, ip):
            limit = _limit_for_category(category)
            row = conn.execute(
                "SELECT attempts,first_failed_at,locked_until FROM auth_login_failures "
                "WHERE key=?",
                (key,),
            ).fetchone()
            first = _parse_stamp(row["first_failed_at"] if row else None)
            attempts = int(row["attempts"] or 0) if row else 0
            if first is None or (now - first).total_seconds() > window:
                attempts = 0
                first = now
            attempts += 1
            locked = _stamp(now + timedelta(seconds=lock_seconds)) if attempts >= limit else ""
            conn.execute(
                "INSERT INTO auth_login_failures "
                "(key,category,attempts,first_failed_at,last_failed_at,locked_until,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "category=excluded.category,attempts=excluded.attempts,"
                "first_failed_at=excluded.first_failed_at,last_failed_at=excluded.last_failed_at,"
                "locked_until=excluded.locked_until,updated_at=excluded.updated_at",
                (key, category, attempts, _stamp(first), now_text, locked, now_text),
            )
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()


def _clear_login_failures(username: str, *, conn=None) -> None:
    init_auth_schema()
    key, _category = _failure_keys(username, "")[0]
    own_conn = conn is None
    if own_conn:
        conn = connect(DB, timeout=15)
    try:
        conn.execute("DELETE FROM auth_login_failures WHERE key=?", (key,))
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()


def unlock_login(user_id: int, *, actor: str = "") -> bool:
    init_auth_schema()
    with connect(DB, timeout=15) as conn:
        row = conn.execute("SELECT username FROM users WHERE id=?", (int(user_id),)).fetchone()
        if row is None:
            raise AuthError("用户不存在")
        key, _category = _failure_keys(row["username"], "")[0]
        cursor = conn.execute("DELETE FROM auth_login_failures WHERE key=?", (key,))
        unlocked = bool(cursor.rowcount)
    _audit("user.login.unlock", row["username"], f"unlocked={unlocked}", actor=actor)
    return unlocked


def _totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_code(secret: str, counter: int) -> str:
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret.upper() + padding)
    digest = hmac.new(key, int(counter).to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(value % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def _matched_totp_counter(secret: str, code: str, *, after: int = 0) -> int | None:
    normalized = re.sub(r"\s+", "", str(code or ""))
    if not re.fullmatch(r"\d{%d}" % TOTP_DIGITS, normalized):
        return None
    current = int(time.time()) // TOTP_INTERVAL
    for counter in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        if counter > int(after or 0) and hmac.compare_digest(
            _totp_code(secret, counter), normalized
        ):
            return counter
    return None


def _mfa_aad(user_id: int, username: str) -> str:
    return f"yunce-mfa:{int(user_id)}:{username}"


def _encrypt_mfa_secret(user_id: int, username: str, secret: str) -> str:
    from utils.credential_crypto import encrypt_bytes

    encrypted = encrypt_bytes(secret.encode("ascii"), _mfa_aad(user_id, username))
    return base64.urlsafe_b64encode(encrypted).decode("ascii")


def _decrypt_mfa_secret(user_id: int, username: str, encoded: str) -> str:
    from utils.credential_crypto import decrypt_bytes

    encrypted = base64.urlsafe_b64decode(str(encoded or "").encode("ascii"))
    return decrypt_bytes(encrypted, _mfa_aad(user_id, username)).decode("ascii")


def _otpauth_uri(username: str, secret: str) -> str:
    label = quote(f"YunCe:{username}", safe="")
    issuer = quote("YunCe", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_INTERVAL}"
    )


def _mfa_qr_data_url(uri: str) -> str:
    import io

    try:
        import qrcode

        image = qrcode.make(uri)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except Exception:
        try:
            import segno

            output = io.BytesIO()
            segno.make(uri, error="m").save(output, kind="png", scale=6, border=2)
            return "data:image/png;base64," + base64.b64encode(
                output.getvalue()
            ).decode("ascii")
        except Exception:
            logger.exception("生成双因素认证二维码失败")
            return ""


def _verify_active_totp(conn, row: Any, code: str) -> int:
    if not bool(row["mfa_enabled"]) or not row["mfa_secret"]:
        raise AuthError("双因素认证未启用")
    try:
        secret = _decrypt_mfa_secret(int(row["id"]), str(row["username"]), row["mfa_secret"])
    except (ValueError, TypeError, binascii.Error) as exc:
        raise AuthError("双因素密钥不可用，请联系管理员重置") from exc
    counter = _matched_totp_counter(secret, code, after=int(row["mfa_last_counter"] or 0))
    if counter is None:
        raise AuthError("双因素验证码不正确或已使用")
    cursor = conn.execute(
        "UPDATE users SET mfa_last_counter=?,updated_at=? "
        "WHERE id=? AND mfa_last_counter<?",
        (counter, _stamp(), int(row["id"]), counter),
    )
    if not cursor.rowcount:
        raise AuthError("双因素验证码已使用，请等待下一个验证码")
    return counter


def setup_mfa(username: str, current_password: str) -> dict:
    init_auth_schema()
    with connect(DB, timeout=30) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None or row["disabled"] or not verify_password(
            current_password, row["password_hash"]
        ):
            raise AuthError("当前密码不正确")
        secret = _totp_secret()
        try:
            encrypted_secret = _encrypt_mfa_secret(int(row["id"]), username, secret)
        except (OSError, RuntimeError, ValueError) as exc:
            raise AuthError("凭据主密钥不可用，无法安全保存双因素密钥") from exc
        conn.execute(
            "UPDATE users SET mfa_pending_secret=?,mfa_enabled=0,updated_at=? WHERE id=?",
            (encrypted_secret, _stamp(), int(row["id"])),
        )
    uri = _otpauth_uri(username, secret)
    _audit("auth.mfa.setup", username, "stage=pending", actor=username)
    return {
        "secret": secret,
        "otpauthUri": uri,
        "qrDataUrl": _mfa_qr_data_url(uri),
        "digits": TOTP_DIGITS,
        "period": TOTP_INTERVAL,
    }


def _revoke_user_refresh_tokens(conn, user_id: int, keep_session_id: str = "") -> int:
    """同一连接内撤销刷新令牌，避免"会话撤了、刷新还能换新票"的口子。"""
    sql = (
        "UPDATE session_refresh_tokens SET revoked_at=? "
        "WHERE user_id=? AND revoked_at IS NULL"
    )
    params: list[Any] = [_stamp(), int(user_id)]
    if keep_session_id:
        sql += " AND session_id<>?"
        params.append(keep_session_id)
    cursor = conn.execute(sql, tuple(params))
    return int(cursor.rowcount or 0)


def _revoke_other_sessions(conn, user_id: int, keep_session_id: str = "") -> int:
    sql = "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL"
    params: list[Any] = [_stamp(), int(user_id)]
    if keep_session_id:
        sql += " AND id<>?"
        params.append(keep_session_id)
    cursor = conn.execute(sql, tuple(params))
    _revoke_user_refresh_tokens(conn, int(user_id), keep_session_id)
    return int(cursor.rowcount or 0)


def confirm_mfa(username: str, code: str, *, keep_session_id: str = "") -> dict:
    init_auth_schema()
    with connect(DB, timeout=30) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None or row["disabled"] or not row["mfa_pending_secret"]:
            raise AuthError("没有待确认的双因素认证设置")
        try:
            secret = _decrypt_mfa_secret(
                int(row["id"]), username, row["mfa_pending_secret"]
            )
        except (ValueError, TypeError, binascii.Error) as exc:
            raise AuthError("双因素密钥不可用，请重新设置") from exc
        counter = _matched_totp_counter(secret, code)
        if counter is None:
            raise AuthError("双因素验证码不正确")
        conn.execute(
            "UPDATE users SET mfa_secret=?,mfa_pending_secret='',mfa_enabled=1,"
            "mfa_confirmed_at=?,mfa_last_counter=?,updated_at=? WHERE id=?",
            (
                row["mfa_pending_secret"], _stamp(), counter, _stamp(), int(row["id"]),
            ),
        )
        revoked = _revoke_other_sessions(conn, int(row["id"]), keep_session_id)
        updated = conn.execute("SELECT * FROM users WHERE id=?", (int(row["id"]),)).fetchone()
    _audit("auth.mfa.enable", username, f"other_sessions_revoked={revoked}", actor=username)
    return _user_public(updated)


def disable_mfa(username: str, current_password: str, code: str, *, keep_session_id: str = "") -> dict:
    init_auth_schema()
    with connect(DB, timeout=30) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None or row["disabled"] or not verify_password(
            current_password, row["password_hash"]
        ):
            raise AuthError("当前密码不正确")
        _verify_active_totp(conn, row, code)
        conn.execute(
            "UPDATE users SET mfa_enabled=0,mfa_secret='',mfa_pending_secret='',"
            "mfa_confirmed_at=NULL,mfa_last_counter=0,updated_at=? WHERE id=?",
            (_stamp(), int(row["id"])),
        )
        revoked = _revoke_other_sessions(conn, int(row["id"]), keep_session_id)
        updated = conn.execute("SELECT * FROM users WHERE id=?", (int(row["id"]),)).fetchone()
    _audit("auth.mfa.disable", username, f"other_sessions_revoked={revoked}", actor=username)
    return _user_public(updated)


def reset_mfa(user_id: int, *, actor: str = "") -> dict:
    init_auth_schema()
    with connect(DB, timeout=30) as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
        if row is None:
            raise AuthError("用户不存在")
        conn.execute(
            "UPDATE users SET mfa_enabled=0,mfa_secret='',mfa_pending_secret='',"
            "mfa_confirmed_at=NULL,mfa_last_counter=0,updated_at=? WHERE id=?",
            (_stamp(), int(user_id)),
        )
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (_stamp(), int(user_id)),
        )
        updated = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
    _audit("user.mfa.reset", updated["username"], "sessions_revoked=1", actor=actor)
    return _user_public(updated)


def create_user(
    username: str,
    password: str,
    role: str,
    scope: Any,
    *,
    display_name: str = "",
    must_change_password: bool = True,
) -> dict:
    init_auth_schema()
    normalized_user = _normalize_username(username)
    normalized_role = _validate_role(role)
    normalized_scope = _scope_value(scope)
    if normalized_role != runtime_config.ROLE_ADMIN and not normalized_scope:
        raise AuthError("非管理员必须配置至少一个账号 scope")
    now = _stamp()
    with connect(DB, timeout=30) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO users (username,display_name,password_hash,role,scope,"
                "disabled,must_change_password,created_at,updated_at,password_changed_at) "
                "VALUES (?,?,?,?,?,0,?,?,?,?)",
                (
                    normalized_user,
                    str(display_name or "").strip()[:100],
                    _hash_password(password),
                    normalized_role,
                    json.dumps(normalized_scope, ensure_ascii=False),
                    1 if must_change_password else 0,
                    now,
                    now,
                    now,
                ),
            )
            user_id = int(cursor.lastrowid)
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise AuthError("用户名已存在") from exc
            raise
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    result = _user_public(row)
    _audit("user.create", normalized_user, f"role={normalized_role}")
    return result


def list_users() -> list[dict]:
    init_auth_schema()
    with connect(DB, timeout=30) as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [_user_public(row) for row in rows]


def get_user(user_id: int) -> dict | None:
    init_auth_schema()
    with connect(DB, timeout=30) as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
    return _user_public(row) if row else None


def update_user(
    user_id: int,
    *,
    role: str | None = None,
    scope: Any = None,
    disabled: bool | None = None,
    display_name: str | None = None,
) -> dict:
    init_auth_schema()
    with connect(DB, timeout=30) as conn:
        current = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
        if current is None:
            raise AuthError("用户不存在")
        normalized_role = _validate_role(role) if role is not None else current["role"]
        normalized_scope = (
            _scope_value(scope) if scope is not None else _scope_value(current["scope"])
        )
        next_disabled = bool(current["disabled"]) if disabled is None else bool(disabled)
        if normalized_role != runtime_config.ROLE_ADMIN and not normalized_scope:
            raise AuthError("非管理员必须配置至少一个账号 scope")
        removes_last_admin = (
            current["role"] == runtime_config.ROLE_ADMIN
            and current["disabled"] == 0
            and (normalized_role != runtime_config.ROLE_ADMIN or next_disabled)
            and _active_admin_count(conn, excluding=int(user_id)) == 0
        )
        if removes_last_admin:
            raise AuthError("不能禁用或降级最后一个有效管理员")
        next_display = (
            str(display_name or "").strip()[:100]
            if display_name is not None else current["display_name"]
        )
        conn.execute(
            "UPDATE users SET display_name=?,role=?,scope=?,disabled=?,updated_at=? "
            "WHERE id=?",
            (
                next_display,
                normalized_role,
                json.dumps(normalized_scope, ensure_ascii=False),
                1 if next_disabled else 0,
                _stamp(),
                int(user_id),
            ),
        )
        if next_disabled or normalized_role != current["role"]:
            conn.execute(
                "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (_stamp(), int(user_id)),
            )
            _revoke_user_refresh_tokens(conn, int(user_id))
        row = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
    result = _user_public(row)
    _audit("user.update", result["username"], f"role={result['role']};disabled={result['disabled']}")
    return result


def revoke_sessions(user_id: int, *, actor: str = "") -> int:
    init_auth_schema()
    with connect(DB, timeout=30) as conn:
        cursor = conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (_stamp(), int(user_id)),
        )
        count = int(cursor.rowcount or 0)
        _revoke_user_refresh_tokens(conn, int(user_id))
    _audit("user.sessions.revoke", str(user_id), f"count={count}", actor=actor)
    return count


def reset_password(user_id: int, new_password: str, *, must_change: bool = True) -> dict:
    init_auth_schema()
    encoded = _hash_password(new_password)
    now = _stamp()
    with connect(DB, timeout=30) as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
        if row is None:
            raise AuthError("用户不存在")
        conn.execute(
            "UPDATE users SET password_hash=?,password_changed_at=?,updated_at=?,"
            "must_change_password=? WHERE id=?",
            (encoded, now, now, 1 if must_change else 0, int(user_id)),
        )
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now, int(user_id)),
        )
        _revoke_user_refresh_tokens(conn, int(user_id))
        updated = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
    _audit("user.password.reset", updated["username"], "sessions_revoked=1")
    return _user_public(updated)


def change_own_password(username: str, current_password: str, new_password: str) -> None:
    init_auth_schema()
    encoded = _hash_password(new_password)
    now = _stamp()
    with connect(DB, timeout=30) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None or row["disabled"] or not verify_password(current_password, row["password_hash"]):
            raise AuthError("当前密码不正确")
        conn.execute(
            "UPDATE users SET password_hash=?,password_changed_at=?,updated_at=?,"
            "must_change_password=0 WHERE id=?",
            (encoded, now, now, int(row["id"])),
        )
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now, int(row["id"])),
        )
        _revoke_user_refresh_tokens(conn, int(row["id"]))
    _audit("user.password.change", username, "all_sessions_revoked=1", actor=username)


def authenticate_session(raw_token: str) -> dict | None:
    # 实名登录或单密码门禁都会签发会话，两种模式都要能校验
    if not bool(runtime_config.PERSONAL_AUTH or runtime_config.password_gate_enabled()):
        return None
    token = str(raw_token or "").strip()
    if not token:
        return None
    init_auth_schema()
    token_hash = hash_session_token(token)
    now = _now()
    with connect(DB, timeout=15) as conn:
        row = conn.execute(
            "SELECT s.id AS session_id,s.created_at AS session_created_at,"
            "s.last_seen_at,s.expires_at,s.revoked_at,s.ip,s.user_agent,u.* "
            "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        expiry = _parse_stamp(row["expires_at"])
        revoked = _parse_stamp(row["revoked_at"])
        if bool(row["disabled"]) or revoked is not None or expiry is None or expiry <= now:
            if revoked is None:
                conn.execute(
                    "UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (_stamp(now), token_hash),
                )
            return None
        seen = _parse_stamp(row["last_seen_at"])
        if seen is None or (now - seen).total_seconds() >= SESSION_TOUCH_SECONDS:
            conn.execute(
                "UPDATE sessions SET last_seen_at=? WHERE id=?",
                (_stamp(now), token_hash),
            )
        _slide_session(conn, row, now, token_hash, expiry)
    identity = _identity_for_user(row)
    return identity or None


def _slide_session(conn, row: Any, now: datetime, token_hash: str, expiry: datetime) -> bool:
    """滑动续期：剩余不足一半就把窗口推回完整 TTL，最多推到绝对上限。

    历史实现只在签发时写一次 ``expires_at``，``last_seen_at`` 无论怎么更新都不会
    延长有效期——用户连续工作到第 12 小时就会被踢回登录页，长上传任务尤其难受。
    """
    if not SESSION_SLIDING_ENABLED:
        return False
    ttl = timedelta(hours=max(1, int(SESSION_TTL_HOURS)))
    remaining = (expiry - now).total_seconds()
    if remaining > ttl.total_seconds() * float(SESSION_SLIDE_RATIO):
        return False
    created = _parse_stamp(row["session_created_at"]) or now
    absolute = created + timedelta(days=max(1, int(SESSION_ABSOLUTE_DAYS)))
    target = min(now + ttl, absolute)
    if target <= expiry:
        return False
    conn.execute(
        "UPDATE sessions SET expires_at=? WHERE id=?",
        (_stamp(target), token_hash),
    )
    return True


def refresh_session(raw_token: str) -> dict:
    """用刷新令牌换一套新的 access + refresh（旋转 + 复用检测）。

    复用检测：一枚**已作废**的刷新令牌再次被使用时，认为它可能已经泄漏
    （正常客户端旋转后拿到的是新令牌），于是把该用户的全部会话一起撤销，
    逼所有设备重新走一次登录——这是 OAuth2 里 refresh-token rotation 的标准做法。
    """
    if not bool(runtime_config.PERSONAL_AUTH or runtime_config.password_gate_enabled()):
        raise AuthError("登录未启用")
    raw = str(raw_token or "").strip()
    if not raw:
        raise AuthError("缺少刷新令牌，请重新登录")
    init_auth_schema()
    now = _now()
    token_hash = hash_session_token(raw)
    reused = False
    with connect(DB, timeout=30) as conn:
        row = conn.execute(
            "SELECT t.id AS token_id,t.expires_at AS token_expires,"
            "t.revoked_at AS token_revoked,t.session_id AS token_session,u.* "
            "FROM session_refresh_tokens t JOIN users u ON u.id=t.user_id WHERE t.id=?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise AuthError("刷新令牌无效，请重新登录")
        expires = _parse_stamp(row["token_expires"])
        revoked = _parse_stamp(row["token_revoked"])
        username = str(row["username"] or "")
        if revoked is not None:
            reused = True
            conn.execute(
                "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (_stamp(now), int(row["id"])),
            )
            conn.execute(
                "UPDATE session_refresh_tokens SET revoked_at=? "
                "WHERE user_id=? AND revoked_at IS NULL",
                (_stamp(now), int(row["id"])),
            )
        elif expires is None or expires <= now or bool(row["disabled"]):
            raise AuthError("刷新令牌已过期，请重新登录")
        else:
            conn.execute(
                "UPDATE session_refresh_tokens SET rotated_at=?,revoked_at=? WHERE id=?",
                (_stamp(now), _stamp(now), token_hash),
            )
            old_session = str(row["token_session"] or "")
            current = conn.execute(
                "SELECT * FROM users WHERE id=?", (int(row["id"]),),
            ).fetchone()
            result = _issue_session(conn, current, now)
            if old_session:
                conn.execute(
                    "UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (_stamp(now), old_session),
                )
    if reused:
        _audit("auth.refresh.reuse", username, "all_sessions_revoked=1", actor=username)
        raise AuthError("刷新令牌已失效，请重新登录")
    _audit("auth.refresh", username, "result=accepted", actor=username)
    return result


def revoke_refresh_tokens(
    raw_token: str = "",
    session_id: str = "",
    user_id: int | None = None,
) -> int:
    """撤销刷新令牌：登出按令牌/会话，改密、禁用、改角色按用户。"""
    init_auth_schema()
    now = _stamp()
    revoked = 0
    with connect(DB, timeout=15) as conn:
        if raw_token:
            cursor = conn.execute(
                "UPDATE session_refresh_tokens SET revoked_at=? "
                "WHERE id=? AND revoked_at IS NULL",
                (now, hash_session_token(raw_token)),
            )
            revoked += int(cursor.rowcount or 0)
        if session_id:
            cursor = conn.execute(
                "UPDATE session_refresh_tokens SET revoked_at=? "
                "WHERE session_id=? AND revoked_at IS NULL",
                (now, session_id),
            )
            revoked += int(cursor.rowcount or 0)
        if user_id:
            cursor = conn.execute(
                "UPDATE session_refresh_tokens SET revoked_at=? "
                "WHERE user_id=? AND revoked_at IS NULL",
                (now, int(user_id)),
            )
            revoked += int(cursor.rowcount or 0)
    return revoked


def _attach_refresh_cookie(response, result: dict | None):
    """把刷新令牌写进 HttpOnly Cookie；跨域开发环境由前端回落到显式字段。"""
    token = str((result or {}).get("refreshToken") or "")
    if not token:
        return response
    max_age = max(60, int(REFRESH_TTL_DAYS * 86400))
    try:
        response.set_cookie(
            REFRESH_COOKIE_NAME, token,
            max_age=max_age, httponly=True, samesite="Lax",
            secure=bool(request.is_secure), path=REFRESH_COOKIE_PATH,
        )
    except Exception:
        pass
    return response


def _clear_refresh_cookie(response):
    try:
        response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)
    except Exception:
        pass
    return response


def _issue_session(conn, row: Any, now: datetime) -> dict:
    raw_token = secrets.token_urlsafe(48)
    token_hash = hash_session_token(raw_token)
    expires = _stamp(now + timedelta(hours=max(1, int(SESSION_TTL_HOURS))))
    conn.execute(
        "INSERT INTO sessions (id,user_id,created_at,last_seen_at,expires_at,ip,user_agent) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            token_hash,
            int(row["id"]),
            _stamp(now),
            _stamp(now),
            expires,
            _request_ip(),
            _user_agent(),
        ),
    )
    refresh = _issue_refresh_token(conn, int(row["id"]), token_hash, now)
    conn.execute(
        "UPDATE users SET last_login_at=?,updated_at=? WHERE id=?",
        (_stamp(now), _stamp(now), int(row["id"])),
    )
    live = conn.execute(
        "SELECT id FROM sessions WHERE user_id=? AND revoked_at IS NULL "
        "AND expires_at>? ORDER BY created_at DESC",
        (int(row["id"]), _stamp(now)),
    ).fetchall()
    for stale in live[max(1, int(MAX_SESSIONS_PER_USER)):]:
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE id=?",
            (_stamp(now), stale["id"]),
        )
    return {
        "token": raw_token,
        "expiresAt": expires,
        "refreshToken": refresh["token"],
        "refreshExpiresAt": refresh["expiresAt"],
        "identity": _identity_for_user(row),
        "mustChangePassword": bool(row["must_change_password"]),
        "mfaRequired": False,
    }


def _issue_refresh_token(conn, user_id: int, session_id: str, now: datetime) -> dict:
    """签发一枚刷新令牌；只把哈希落库，明文仅出现在响应里。"""
    raw = secrets.token_urlsafe(48)
    expires = _stamp(now + timedelta(days=max(1, int(REFRESH_TTL_DAYS))))
    conn.execute(
        "INSERT INTO session_refresh_tokens "
        "(id,user_id,session_id,created_at,expires_at,ip,user_agent) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            hash_session_token(raw), int(user_id), str(session_id or ""),
            _stamp(now), expires, _request_ip(), _user_agent(),
        ),
    )
    # 顺手清理该用户已经过期/作废太久的令牌，避免表无限增长。
    conn.execute(
        "DELETE FROM session_refresh_tokens WHERE user_id=? AND "
        "(expires_at<=? OR (revoked_at IS NOT NULL AND revoked_at<=?))",
        (int(user_id), _stamp(now), _stamp(now - timedelta(days=7))),
    )
    return {"token": raw, "expiresAt": expires}


def _issue_mfa_challenge(conn, row: Any, now: datetime) -> dict:
    raw = secrets.token_urlsafe(40)
    expires = _stamp(now + timedelta(seconds=int(MFA_CHALLENGE_TTL_SECONDS)))
    conn.execute("DELETE FROM mfa_challenges WHERE expires_at<=? OR used_at IS NOT NULL", (_stamp(now),))
    conn.execute(
        "DELETE FROM mfa_challenges WHERE user_id=? AND used_at IS NULL",
        (int(row["id"]),),
    )
    conn.execute(
        "INSERT INTO mfa_challenges "
        "(id,user_id,created_at,expires_at,attempts,ip,user_agent) "
        "VALUES (?,?,?,?,0,?,?)",
        (
            hash_session_token(raw), int(row["id"]), _stamp(now), expires,
            _request_ip(), _user_agent(),
        ),
    )
    return {
        "mfaRequired": True,
        "challengeToken": raw,
        "expiresAt": expires,
        "user": row["username"],
    }


def login(username: str, password: str) -> dict:
    gate_enabled = bool(runtime_config.password_gate_enabled())
    if not bool(runtime_config.PERSONAL_AUTH) and not gate_enabled:
        raise AuthError("实名登录未启用")
    normalized_user = str(username or "").strip()
    if not normalized_user or not password:
        raise AuthError("用户名或密码不正确")
    init_auth_schema()
    ip = _request_ip()
    check_login_lock(normalized_user, ip)
    now = _now()
    with connect(DB, timeout=30) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (normalized_user,)).fetchone()
        valid = bool(
            row
            and not row["disabled"]
            and verify_password(password, row["password_hash"])
        )
        if not valid:
            row = None
        else:
            if not str(row["password_hash"]).startswith(PASSWORD_SCHEME + "$"):
                conn.execute(
                    "UPDATE users SET password_hash=?,password_changed_at=?,updated_at=? "
                    "WHERE id=?",
                    (_hash_password(password), _stamp(now), _stamp(now), int(row["id"])),
                )
            _clear_login_failures(normalized_user, conn=conn)
            if bool(row["mfa_enabled"]):
                result = _issue_mfa_challenge(conn, row, now)
            else:
                result = _issue_session(conn, row, now)
    if row is None:
        _record_login_failure(normalized_user, ip)
        _audit("auth.login", normalized_user, f"result=rejected;ip={ip}", actor="anonymous")
        raise AuthError("用户名或密码不正确")
    _audit(
        "auth.login.password", normalized_user,
        f"result=accepted;mfa_required={str(bool(row['mfa_enabled'])).lower()};ip={ip}",
        actor=normalized_user,
    )
    return result


LOCAL_GATE_USERNAME = "local"


def ensure_password_gate_user() -> bool:
    """把 ``YUNCE_ACCESS_PASSWORD`` 同步成一个本地管理员账号。

    为什么要落成一个"账号"：会话、刷新令牌、注销、改密失效这些机制都已经在
    ``sessions`` 表里跑通了。单密码门禁只需要"密码 → 会话"，没必要另起一套
    会话表，所以这里把配置里的密码同步到一个固定用户名 ``local`` 的管理员上，
    改 .env 里的密码后重启就会同步过来。
    """
    password = str(runtime_config.ACCESS_PASSWORD or "").strip()
    if not password:
        return False
    init_auth_schema()
    now = _stamp()
    with connect(DB, timeout=30) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (LOCAL_GATE_USERNAME,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username,display_name,password_hash,role,scope,"
                "disabled,must_change_password,created_at,updated_at,password_changed_at,mfa_enabled) "
                "VALUES (?,?,?,?,?,0,0,?,?,?,0)",
                (
                    LOCAL_GATE_USERNAME, "本机管理员",
                    _hash_password(password, allow_short=True),
                    runtime_config.ROLE_ADMIN, "[]", now, now, now,
                ),
            )
        elif (
            bool(row["disabled"])
            or str(row["role"]) != runtime_config.ROLE_ADMIN
            or not verify_password(password, row["password_hash"])
        ):
            conn.execute(
                "UPDATE users SET password_hash=?,role=?,scope='[]',disabled=0,"
                "must_change_password=0,updated_at=?,password_changed_at=? WHERE id=?",
                (
                    _hash_password(password, allow_short=True),
                    runtime_config.ROLE_ADMIN, now, now, int(row["id"]),
                ),
            )
    # 改密码后不应该还被上一次的失败次数锁住
    try:
        _clear_login_failures(LOCAL_GATE_USERNAME)
    except Exception:
        pass
    return True


def password_gate_login(password: str) -> dict:
    """单密码登录：不需要用户名，配置里的密码对了就签发会话。"""
    if not runtime_config.password_gate_enabled():
        raise AuthError("密码登录未启用")
    ensure_password_gate_user()
    try:
        return login(LOCAL_GATE_USERNAME, password)
    except AuthError as exc:
        text = str(exc)
        # 门禁模式没有用户名，提示里不该出现"用户名"
        if "用户名或密码不正确" in text:
            raise AuthError("密码不正确") from exc
        raise


def verify_mfa_login(challenge_token: str, code: str) -> dict:
    if not bool(runtime_config.PERSONAL_AUTH):
        raise AuthError("实名登录未启用")
    raw = str(challenge_token or "").strip()
    if not raw:
        raise AuthError("双因素登录挑战无效或已过期")
    init_auth_schema()
    now = _now()
    mfa_error: AuthError | None = None
    audit_user = ""
    with connect(DB, timeout=30) as conn:
        row = conn.execute(
            "SELECT c.id AS challenge_id,c.attempts AS challenge_attempts,"
            "c.expires_at AS challenge_expires_at,c.used_at,c.ip AS challenge_ip,u.* "
            "FROM mfa_challenges c JOIN users u ON u.id=c.user_id WHERE c.id=?",
            (hash_session_token(raw),),
        ).fetchone()
        expired = (
            row is None
            or bool(row["disabled"])
            or row["used_at"] is not None
            or (_parse_stamp(row["challenge_expires_at"]) or datetime.min) <= now
            or int(row["challenge_attempts"] or 0) >= MFA_MAX_ATTEMPTS
            or (row["challenge_ip"] and row["challenge_ip"] != _request_ip())
        )
        if expired:
            raise AuthError("双因素登录挑战无效或已过期")
        check_login_lock(str(row["username"]), _request_ip(), conn=conn)
        audit_user = str(row["username"])
        try:
            _verify_active_totp(conn, row, code)
        except AuthError as exc:
            attempts = int(row["challenge_attempts"] or 0) + 1
            conn.execute(
                "UPDATE mfa_challenges SET attempts=?,used_at=? WHERE id=?",
                (
                    attempts,
                    _stamp(now) if attempts >= MFA_MAX_ATTEMPTS else None,
                    row["challenge_id"],
                ),
            )
            conn.execute("UPDATE users SET updated_at=? WHERE id=?", (_stamp(now), int(row["id"])))
            _record_login_failure(str(row["username"]), _request_ip(), conn=conn)
            mfa_error = exc
        if mfa_error is None:
            conn.execute("UPDATE mfa_challenges SET used_at=? WHERE id=?", (_stamp(now), row["challenge_id"]))
            _clear_login_failures(str(row["username"]), conn=conn)
            current = conn.execute("SELECT * FROM users WHERE id=?", (int(row["id"]),)).fetchone()
            result = _issue_session(conn, current, now)
    if mfa_error is not None:
        _audit(
            "auth.mfa.verify", audit_user,
            f"result=rejected;ip={_request_ip()}", actor=audit_user,
        )
        raise mfa_error
    _audit(
        "auth.mfa.verify", row["username"],
        f"result=accepted;ip={_request_ip()}", actor=row["username"],
    )
    return result


def logout(raw_token: str) -> bool:
    if not raw_token:
        return False
    init_auth_schema()
    with connect(DB, timeout=15) as conn:
        cursor = conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (_stamp(), hash_session_token(raw_token)),
        )
        return bool(cursor.rowcount)


def auth_status() -> dict:
    init_auth_schema()
    with connect(DB, timeout=15) as conn:
        total = int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] or 0)
        active = int(
            conn.execute("SELECT COUNT(*) AS n FROM users WHERE disabled=0").fetchone()["n"] or 0
        )
        admins = _active_admin_count(conn)
        live_sessions = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE revoked_at IS NULL AND expires_at>?",
                (_stamp(),),
            ).fetchone()["n"] or 0
        )
        mfa_users = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE disabled=0 AND mfa_enabled=1"
            ).fetchone()["n"] or 0
        )
        mfa_admins = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE disabled=0 AND role='admin' "
                "AND mfa_enabled=1"
            ).fetchone()["n"] or 0
        )
        locked_accounts = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM auth_login_failures "
                "WHERE category='user' AND locked_until>?",
                (_stamp(),),
            ).fetchone()["n"] or 0
        )
    return {
        "personalAuthEnabled": bool(runtime_config.PERSONAL_AUTH),
        "tokenFallbackEnabled": bool(runtime_config.ACCESS_TOKEN or runtime_config.TOKENS_FILE),
        "users": total,
        "activeUsers": active,
        "activeAdmins": admins,
        "liveSessions": live_sessions,
        "mfaEnabledUsers": mfa_users,
        "mfaEnabledAdmins": mfa_admins,
        "lockedAccounts": locked_accounts,
        "sessionTtlHours": int(SESSION_TTL_HOURS),
        "passwordMinLength": PASSWORD_MIN_LENGTH,
        "mfaChallengeTtlSeconds": int(MFA_CHALLENGE_TTL_SECONDS),
    }


def _json_error(message: str, status: int = 400):
    return jsonify({"code": status, "msg": message, "data": None}), status


@bp.get("/api/v2/auth/config")
def auth_config():
    status = auth_status()
    return jsonify({
        "code": 200,
        "msg": None,
        "data": {
            "personalAuthEnabled": status["personalAuthEnabled"],
            "tokenFallbackEnabled": status["tokenFallbackEnabled"],
            "sessionTtlHours": status["sessionTtlHours"],
            "passwordMinLength": status["passwordMinLength"],
            "mfaAvailable": status["personalAuthEnabled"],
            "mfaChallengeTtlSeconds": status["mfaChallengeTtlSeconds"],
            # 会话保持能力：滑动续期 + 刷新令牌（前端据此决定是否启用静默续期）
            "sessionSlidingEnabled": bool(SESSION_SLIDING_ENABLED),
            "sessionAbsoluteDays": int(SESSION_ABSOLUTE_DAYS),
            "refreshTtlDays": int(REFRESH_TTL_DAYS),
            "refreshEnabled": bool(
                runtime_config.PERSONAL_AUTH or runtime_config.password_gate_enabled()
            ),
            # 单密码门禁：前端据此只显示"密码"一个输入框
            "passwordGateEnabled": bool(runtime_config.password_gate_enabled()),
        },
    }), 200


@bp.get("/api/v2/auth/status")
def auth_status_route():
    return jsonify({"code": 200, "msg": None, "data": auth_status()}), 200


@bp.post("/api/v2/auth/login")
def auth_login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    from utils.request_limiter import allow, request_key

    def _env_int(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)) or default)
        except (TypeError, ValueError):
            return default
        return max(1, value)

    window = _env_int("FLOWOPS_LOGIN_ATTEMPT_WINDOW", 300)
    ip_ok, ip_wait = allow(
        "named-auth-login-ip",
        limit=_env_int("FLOWOPS_LOGIN_IP_LIMIT", 60),
        window_seconds=window,
        key=request_key(),
    )
    user_ok, user_wait = allow(
        "named-auth-login-user",
        limit=_env_int("FLOWOPS_LOGIN_ATTEMPT_LIMIT", 10),
        window_seconds=window,
        key="user:" + username.casefold()[:64],
    )
    if not ip_ok or not user_ok:
        wait = max(ip_wait, user_wait, 1)
        response = jsonify({"code": 429, "msg": "登录尝试过于频繁，请稍后重试", "data": None})
        response.status_code = 429
        response.headers["Retry-After"] = str(wait)
        return response
    try:
        result = login(
            username,
            str(payload.get("password") or ""),
        )
    except LoginLockedError as exc:
        response = jsonify({"code": 429, "msg": str(exc), "data": None})
        response.status_code = 429
        response.headers["Retry-After"] = str(exc.retry_after)
        return response
    except AuthError as exc:
        return _json_error(str(exc), 401)
    response = jsonify({"code": 200, "msg": None, "data": result})
    return _attach_refresh_cookie(response, result), 200


@bp.post("/api/v2/auth/unlock")
def auth_unlock():
    """单密码门禁：只验一个密码，不需要用户名。

    密码来自 ``.env`` 的 ``YUNCE_ACCESS_PASSWORD``（使用者自己定义）。校验通过后
    走的是和实名登录完全相同的会话/刷新令牌流程，所以滑动续期、注销、改密失效
    这些能力一个都不少。
    """
    if not runtime_config.password_gate_enabled():
        return _json_error("密码登录未启用", 400)
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password") or "")
    from utils.request_limiter import allow, request_key

    def _env_int(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)) or default)
        except (TypeError, ValueError):
            return default
        return max(1, value)

    window = _env_int("FLOWOPS_LOGIN_ATTEMPT_WINDOW", 300)
    ip_ok, ip_wait = allow(
        "password-gate-ip",
        limit=_env_int("FLOWOPS_LOGIN_IP_LIMIT", 60),
        window_seconds=window,
        key=request_key(),
    )
    if not ip_ok:
        response = jsonify({"code": 429, "msg": "尝试过于频繁，请稍后重试", "data": None})
        response.status_code = 429
        response.headers["Retry-After"] = str(max(int(ip_wait), 1))
        return response
    try:
        result = password_gate_login(password)
    except LoginLockedError as exc:
        response = jsonify({"code": 429, "msg": str(exc), "data": None})
        response.status_code = 429
        response.headers["Retry-After"] = str(exc.retry_after)
        return response
    except AuthError as exc:
        return _json_error(str(exc), 401)
    response = jsonify({"code": 200, "msg": None, "data": result})
    return _attach_refresh_cookie(response, result), 200


@bp.post("/api/v2/auth/refresh")
def auth_refresh():
    """静默续期：refresh（30 天，旋转）→ 新 access（12 小时，可滑动）。

    优先读 HttpOnly Cookie；同源部署时前端根本拿不到明文令牌。
    跨域开发环境（5173 → 5409）浏览器不会带 Cookie，此时允许请求体显式传。
    """
    payload = request.get_json(silent=True) or {}
    raw = str(request.cookies.get(REFRESH_COOKIE_NAME) or payload.get("refreshToken") or "").strip()
    if not raw:
        return _json_error("缺少刷新令牌，请重新登录", 401)
    try:
        result = refresh_session(raw)
    except AuthError as exc:
        response = _json_error(str(exc), 401)
        target = response[0] if isinstance(response, tuple) else response
        _clear_refresh_cookie(target)
        return response
    response = jsonify({"code": 200, "msg": None, "data": result})
    _attach_refresh_cookie(response, result)
    return response, 200


@bp.post("/api/v2/auth/mfa/verify")
def auth_mfa_verify():
    payload = request.get_json(silent=True) or {}
    challenge = str(payload.get("challengeToken") or "").strip()
    from utils.request_limiter import allow, request_key

    window = _env_int("FLOWOPS_LOGIN_ATTEMPT_WINDOW", 300)
    ip_ok, ip_wait = allow(
        "named-auth-mfa-ip",
        limit=_env_int("FLOWOPS_LOGIN_IP_LIMIT", 60),
        window_seconds=window,
        key=request_key(),
    )
    challenge_ok, challenge_wait = allow(
        "named-auth-mfa-challenge",
        limit=_env_int("FLOWOPS_LOGIN_ATTEMPT_LIMIT", 10),
        window_seconds=window,
        key="mfa:" + hash_session_token(challenge)[:64],
    )
    if not ip_ok or not challenge_ok:
        wait = max(ip_wait, challenge_wait, 1)
        response = jsonify({"code": 429, "msg": "验证码尝试过于频繁，请稍后重试", "data": None})
        response.status_code = 429
        response.headers["Retry-After"] = str(wait)
        return response
    try:
        result = verify_mfa_login(challenge, str(payload.get("code") or ""))
    except LoginLockedError as exc:
        response = jsonify({"code": 429, "msg": str(exc), "data": None})
        response.status_code = 429
        response.headers["Retry-After"] = str(exc.retry_after)
        return response
    except AuthError as exc:
        return _json_error(str(exc), 401)
    response = jsonify({"code": 200, "msg": None, "data": result})
    return _attach_refresh_cookie(response, result), 200


def _session_identity_or_error():
    identity = getattr(g, "identity", None) or {}
    if identity.get("source") != "session":
        return None, _json_error("仅实名会话可以使用双因素认证", 400)
    return identity, None


@bp.post("/api/v2/auth/mfa/setup")
def auth_mfa_setup():
    identity, error = _session_identity_or_error()
    if error is not None:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        result = setup_mfa(
            str(identity.get("user") or ""),
            str(payload.get("currentPassword") or ""),
        )
    except AuthError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"code": 200, "msg": None, "data": result}), 200


@bp.post("/api/v2/auth/mfa/confirm")
def auth_mfa_confirm():
    identity, error = _session_identity_or_error()
    if error is not None:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        result = confirm_mfa(
            str(identity.get("user") or ""),
            str(payload.get("code") or ""),
            keep_session_id=str(identity.get("session_id") or ""),
        )
    except AuthError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"code": 200, "msg": None, "data": result}), 200


@bp.post("/api/v2/auth/mfa/disable")
def auth_mfa_disable():
    identity, error = _session_identity_or_error()
    if error is not None:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        result = disable_mfa(
            str(identity.get("user") or ""),
            str(payload.get("currentPassword") or ""),
            str(payload.get("code") or ""),
            keep_session_id=str(identity.get("session_id") or ""),
        )
    except AuthError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"code": 200, "msg": None, "data": result}), 200


@bp.post("/api/v2/auth/logout")
def auth_logout():
    token = runtime_config.extract_token(request.headers, request.args)
    revoked = logout(token)
    refresh = str(request.cookies.get(REFRESH_COOKIE_NAME) or "")
    try:
        revoke_refresh_tokens(
            raw_token=refresh,
            session_id=hash_session_token(token) if token else "",
        )
    except Exception:
        pass
    identity = getattr(g, "identity", None) or {}
    _audit("auth.logout", "", f"revoked={revoked}", actor=identity.get("user") or "")
    response = jsonify({"code": 200, "msg": None, "data": {"revoked": revoked}})
    return _clear_refresh_cookie(response), 200


@bp.post("/api/v2/auth/password")
def auth_change_password():
    identity = getattr(g, "identity", None) or {}
    if identity.get("source") != "session":
        return _json_error("仅实名会话可以修改个人密码", 400)
    payload = request.get_json(silent=True) or {}
    try:
        change_own_password(
            str(identity.get("user") or ""),
            str(payload.get("currentPassword") or ""),
            str(payload.get("newPassword") or ""),
        )
    except AuthError as exc:
        return _json_error(str(exc), 400)
    return jsonify({
        "code": 200,
        "msg": "密码已修改，所有会话已撤销，请重新登录",
        "data": {"reauthenticate": True},
    }), 200


@bp.get("/api/v2/users")
def users_list():
    return jsonify({"code": 200, "msg": None, "data": list_users()}), 200


@bp.post("/api/v2/users")
def users_create():
    payload = request.get_json(silent=True) or {}
    try:
        result = create_user(
            str(payload.get("username") or ""),
            str(payload.get("password") or ""),
            str(payload.get("role") or ""),
            payload.get("scope") or [],
            display_name=str(payload.get("displayName") or ""),
            must_change_password=bool(payload.get("mustChangePassword", True)),
        )
    except AuthError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"code": 200, "msg": None, "data": result}), 200


@bp.patch("/api/v2/users/<int:user_id>")
def users_update(user_id: int):
    payload = request.get_json(silent=True) or {}
    before = get_user(user_id)
    try:
        result = update_user(
            user_id,
            role=payload.get("role") if "role" in payload else None,
            scope=payload.get("scope") if "scope" in payload else None,
            disabled=bool(payload.get("disabled")) if "disabled" in payload else None,
            display_name=(payload.get("displayName")
                          if "displayName" in payload else None),
        )
    except AuthError as exc:
        return _json_error(str(exc), 400)
    access_changed = bool(before) and (
        (not before["disabled"] and result["disabled"])
        or result["role"] != before["role"]
        or list(result["scope"]) != list(before["scope"])
    )
    if access_changed:
        if not before["disabled"] and result["disabled"]:
            reason = f"成员 {result['username']} 已禁用，未开始任务停止执行"
        elif result["role"] != before["role"]:
            reason = (
                f"成员 {result['username']} 角色由 {before['role']} 变更为 "
                f"{result['role']}，未开始任务停止执行"
            )
        else:
            reason = f"成员 {result['username']} 账号范围已变更，未开始任务停止执行"
        try:
            import publish_jobs

            outcome = publish_jobs.cancel_actor_queued_jobs(
                actor_user_id=int(user_id),
                actor_name=str(result["username"]),
                reason=reason,
            )
            result["jobRevocation"] = outcome
            actor = (getattr(g, "identity", None) or {}).get("user") or "system"
            _audit(
                "user.jobs.revoke_account_access",
                str(user_id),
                f"cancelled={outcome.get('cancelled', 0)};"
                f"cancelledItems={outcome.get('cancelledItems', 0)};"
                f"deferred={outcome.get('deferred', 0)}",
                actor=actor,
            )
        except Exception as exc:
            logger.exception("成员 {} 更新后停止待执行任务失败", user_id)
            result["jobRevocationError"] = str(exc)[:300]
            return jsonify({
                "code": 500,
                "msg": "成员权限已更新，但待执行任务停止失败，请立即人工核对任务队列",
                "data": result,
            }), 500
    return jsonify({"code": 200, "msg": None, "data": result}), 200


@bp.get("/api/v2/users/<int:user_id>/queued-jobs")
def users_queued_jobs(user_id: int):
    user = get_user(user_id)
    if user is None:
        return _json_error("用户不存在", 404)
    import publish_jobs

    preview = publish_jobs.cancel_actor_queued_jobs(
        actor_user_id=int(user_id), actor_name=str(user["username"]),
        dry_run=True,
    )
    return jsonify({
        "code": 200,
        "msg": None,
        "data": {
            "user": user["username"],
            "policy": "cancel-unstarted",
            "jobs": preview["jobs"],
            "counts": {
                "matched": preview["matched"],
                "cancellable": preview["cancellable"],
                "deferred": preview["deferred"],
                "pendingItems": preview["pendingItems"],
            },
        },
    }), 200


@bp.post("/api/v2/users/<int:user_id>/cancel-queued-jobs")
def users_cancel_queued_jobs(user_id: int):
    user = get_user(user_id)
    if user is None:
        return _json_error("用户不存在", 404)
    payload = request.get_json(silent=True) or {}
    import publish_jobs

    outcome = publish_jobs.cancel_actor_queued_jobs(
        actor_user_id=int(user_id),
        actor_name=str(user["username"]),
        reason=str(payload.get("reason") or f"管理员停止成员 {user['username']} 的待执行任务"),
        dry_run=bool(payload.get("dryRun", False)),
    )
    if not outcome.get("dryRun"):
        actor = (getattr(g, "identity", None) or {}).get("user") or "system"
        _audit(
            "user.jobs.cancel",
            str(user_id),
            f"cancelled={outcome.get('cancelled', 0)};"
            f"cancelledItems={outcome.get('cancelledItems', 0)};"
            f"deferred={outcome.get('deferred', 0)}",
            actor=actor,
        )
    return jsonify({"code": 200, "msg": None, "data": outcome}), 200


@bp.post("/api/v2/users/<int:user_id>/password")
def users_reset_password(user_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        result = reset_password(
            user_id,
            str(payload.get("password") or ""),
            must_change=bool(payload.get("mustChangePassword", True)),
        )
    except AuthError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"code": 200, "msg": None, "data": result}), 200


@bp.post("/api/v2/users/<int:user_id>/revoke-sessions")
def users_revoke_sessions(user_id: int):
    identity = getattr(g, "identity", None) or {}
    count = revoke_sessions(user_id, actor=identity.get("user") or "")
    return jsonify({"code": 200, "msg": None, "data": {"revoked": count}}), 200


@bp.post("/api/v2/users/<int:user_id>/mfa-reset")
def users_reset_mfa(user_id: int):
    identity = getattr(g, "identity", None) or {}
    try:
        result = reset_mfa(user_id, actor=identity.get("user") or "")
    except AuthError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"code": 200, "msg": None, "data": result}), 200


@bp.post("/api/v2/users/<int:user_id>/unlock-login")
def users_unlock_login(user_id: int):
    identity = getattr(g, "identity", None) or {}
    try:
        unlocked = unlock_login(user_id, actor=identity.get("user") or "")
    except AuthError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"code": 200, "msg": None, "data": {"unlocked": unlocked}}), 200


def register(app) -> None:
    init_auth_schema()
    app.register_blueprint(bp)
