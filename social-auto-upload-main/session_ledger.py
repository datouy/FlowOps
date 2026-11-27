# -*- coding: utf-8 -*-
"""会话账本：记录每次"触达平台"的结果与会话窗口，供保活调度和到期预警使用。

为什么单独一张表
----------------
``user_info`` 只能表达"现在是正常还是异常"，回答不了保活真正要回答的三个问题：

1. 这个账号的登录窗口还剩几天？（抖音 = ``sid_guard`` 里的 60 天固定窗口）
2. 上次"触达"是什么时候、有没有真的刷新设备票？
3. 有没有在窗口耗尽 **之前** 给出过预警？

所以这里只做两件事：**追加写每次触达的证据**，以及**从证据里算出剩余天数**。
判定账号生死仍然由 ``account_store.mark_checked()/mark_probe()`` 负责，
本模块不改 ``user_info``。

字段口径
--------
* ``outcome`` 见 :data:`OUTCOMES`；只有 ``expired`` 才允许判账号失效。
* ``session_*`` 来自 ``sid_guard``（权威有效期），没有该 cookie 的平台留空。
* 时间戳统一写本机本地时间字符串（与 ``user_info.last_check_at`` 一致），
  但 ``daysLeft`` 用 cookie 里的绝对时间换算，不受本地时区影响。
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

from conf import BASE_DIR
from db_utils import connect

#: 每次触达的结论。只有 ``expired`` 会推动账号健康状态。
OUTCOMES = frozenset({
    "renewed",           # sessionid/session 类 cookie 真的被重签
    "device_refreshed",  # 只有设备票（ttwid/odin_tt/bd_ticket_guard_*）被刷新
    "unchanged",         # 页面能打开，但什么 cookie 都没变
    "checked_only",      # 平台暂未实现细粒度探针，只做了存活校验
    "expired",           # 明确的登录失效（登录页 / 失效码）
    "risk",              # 验证码 / 安全验证 / 风控挑战，不代表登录失效
    "network",           # 超时、5xx、DNS 失败等环境问题
    "page_changed",      # 页面结构变化，判定不了
    "window_warning",    # 一次预警事件（不是触达）
    "deferred",          # 本轮不触达（已有常驻通道在维持活跃，例如抖音私信常驻标签页）
    "probe_failed",      # 探针自身异常
})

#: 判定为"续期成功"的 cookie（值变化 = 平台重新签发）
SESSION_COOKIE_NAMES = (
    "sessionid", "sessionid_ss", "sid_tt", "uid_tt", "uid_tt_ss",
    "passport_csrf_token", "sessionid_sr", "web_session", "SESSDATA", "kuaishou.web.cp.api_st",
    "SUB", "BDUSS",
)

#: 证据类 cookie：刷新它们说明"这次访问被平台当真了"，但不延长会话窗口
DEVICE_COOKIE_NAMES = (
    "ttwid", "odin_tt", "s_v_web_id", "msToken", "webid",
    "bd_ticket_guard_consumer_version", "bd_ticket_guard_ticket",
    "bd-ticket-guard-request-tlb-group", "bd-web-sign-page-guard",
    "bd_ticket_guard_pubKey", "d_ticket", "x-user-id-creator.xiaohongshu.com",
    "kuaishou.web.cp.api_ph", "wxuin",
)

SCHEMA_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()


def _base_dir(base_dir: str | Path | None = None) -> Path:
    return Path(base_dir or BASE_DIR).expanduser().resolve()


def db_path(base_dir: str | Path | None = None) -> Path:
    """账本与会话表都放在主库 ``db/database.db``，跟随账号库一起备份。"""
    return _base_dir(base_dir) / "db" / "database.db"


def now_text(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def parse_stamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def init_schema(base_dir: str | Path | None = None) -> None:
    """幂等建表；每个库路径只执行一次。"""
    path = str(db_path(base_dir))
    with SCHEMA_LOCK:
        if path in _INITIALIZED:
            return
        with connect(path, timeout=30) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS account_session_ledger (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id         INTEGER NOT NULL,
                    platform           INTEGER NOT NULL DEFAULT 0,
                    touched_at         TEXT    NOT NULL,
                    source             TEXT    NOT NULL DEFAULT 'keeper',
                    duration_ms        INTEGER NOT NULL DEFAULT 0,
                    outcome            TEXT    NOT NULL DEFAULT '',
                    session_issued_at  TEXT    NOT NULL DEFAULT '',
                    session_expires_at TEXT    NOT NULL DEFAULT '',
                    session_rotated    INTEGER NOT NULL DEFAULT 0,
                    cookie_total       INTEGER NOT NULL DEFAULT 0,
                    cookie_changed     INTEGER NOT NULL DEFAULT 0,
                    egress_ip          TEXT    NOT NULL DEFAULT '',
                    error              TEXT    NOT NULL DEFAULT ''
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_ledger_account "
                "ON account_session_ledger(account_id, touched_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_ledger_outcome "
                "ON account_session_ledger(outcome, touched_at)"
            )
            conn.commit()
        _INITIALIZED.add(path)


# --------------------------------------------------------------------- 写入

def record(
    account_id: int,
    platform: int | str = 0,
    outcome: str = "unchanged",
    *,
    source: str = "keeper",
    duration_ms: int = 0,
    session_issued_at: str = "",
    session_expires_at: str = "",
    session_rotated: bool = False,
    cookie_total: int = 0,
    cookie_changed: int = 0,
    egress_ip: str = "",
    error: str = "",
    base_dir: str | Path | None = None,
) -> int:
    """追加一条触达记录，返回新行 id（失败返回 0，绝不抛异常给调用方）。"""
    result = str(outcome or "").strip()
    if result not in OUTCOMES:
        result = "unchanged"
    try:
        init_schema(base_dir)
        with connect(db_path(base_dir), timeout=30) as conn:
            cursor = conn.execute(
                """INSERT INTO account_session_ledger
                   (account_id, platform, touched_at, source, duration_ms, outcome,
                    session_issued_at, session_expires_at, session_rotated,
                    cookie_total, cookie_changed, egress_ip, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(account_id), int(platform or 0), now_text(), str(source or "keeper")[:32],
                    max(0, int(duration_ms or 0)), result,
                    str(session_issued_at or "")[:32], str(session_expires_at or "")[:32],
                    1 if session_rotated else 0,
                    max(0, int(cookie_total or 0)), max(0, int(cookie_changed or 0)),
                    str(egress_ip or "")[:64], str(error or "")[:400],
                ),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)
    except Exception:
        return 0


# --------------------------------------------------------------------- 读取

def rows(account_id: int, limit: int = 20, base_dir: str | Path | None = None) -> list[dict[str, Any]]:
    try:
        init_schema(base_dir)
        with connect(db_path(base_dir), timeout=15) as conn:
            cursor = conn.execute(
                "SELECT * FROM account_session_ledger WHERE account_id=? "
                "ORDER BY id DESC LIMIT ?",
                (int(account_id), max(1, int(limit or 20))),
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception:
        return []


def latest(account_id: int, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    """最近一次**触达**（预警事件不算，避免把预警时间当成触达时间）。"""
    for row in rows(account_id, limit=12, base_dir=base_dir):
        if row.get("outcome") != "window_warning":
            return row
    return None


def last_touched_at(account_id: int, base_dir: str | Path | None = None) -> datetime | None:
    row = latest(account_id, base_dir=base_dir)
    return parse_stamp(row.get("touched_at")) if row else None


def latest_window_row(account_id: int, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    """最近一条**带会话窗口证据**的记录。

    有些触达拿不到窗口（``checked_only`` / ``deferred`` / 非抖音平台），
    如果直接读最新一行，"剩余天数"会莫名其妙变成空。窗口是慢变量，取最近
    一条有值的记录才是正确语义。
    """
    for row in rows(account_id, limit=30, base_dir=base_dir):
        if str(row.get("session_expires_at") or "").strip():
            return row
    return None


def window_days_left(
    account_id: int,
    base_dir: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> float | None:
    """会话窗口剩余天数；没有 ``sid_guard`` 之类证据时返回 ``None``。"""
    row = latest_window_row(account_id, base_dir=base_dir)
    if not row:
        return None
    expires = parse_stamp(row.get("session_expires_at"))
    if expires is None:
        return None
    return round((expires - (now or datetime.now())).total_seconds() / 86400, 2)


def counts_since(
    account_id: int,
    outcome: str,
    days: int = 30,
    base_dir: str | Path | None = None,
) -> int:
    cutoff = now_text(datetime.now() - timedelta(days=max(1, int(days or 30))))
    try:
        init_schema(base_dir)
        with connect(db_path(base_dir), timeout=15) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM account_session_ledger "
                "WHERE account_id=? AND outcome=? AND touched_at>=?",
                (int(account_id), str(outcome), cutoff),
            ).fetchone()
            return int(row["n"] or 0) if row else 0
    except Exception:
        return 0


def warned_recently(
    account_id: int,
    days: int = 7,
    base_dir: str | Path | None = None,
) -> bool:
    return counts_since(account_id, "window_warning", days=days, base_dir=base_dir) > 0


def state_for(account_id: int, base_dir: str | Path | None = None) -> dict[str, Any]:
    """给前端/接口用的会话摘要。"""
    row = latest(account_id, base_dir=base_dir)
    if row is None:
        return {
            "touchedAt": "", "source": "", "outcome": "", "daysLeft": None,
            "sessionIssuedAt": "", "sessionExpiresAt": "", "sessionRotated": False,
            "renewed30d": 0, "deviceRefreshed30d": 0, "expired30d": 0,
            "error": "", "warningAt": "",
        }
    warning = next(
        (item for item in rows(account_id, limit=12, base_dir=base_dir)
         if item.get("outcome") == "window_warning"),
        None,
    )
    window_row = latest_window_row(account_id, base_dir=base_dir)
    return {
        "touchedAt": row.get("touched_at") or "",
        "source": row.get("source") or "",
        "outcome": row.get("outcome") or "",
        "daysLeft": window_days_left(account_id, base_dir),
        "sessionIssuedAt": (window_row or {}).get("session_issued_at") or "",
        "sessionExpiresAt": (window_row or {}).get("session_expires_at") or "",
        "sessionRotated": bool(row.get("session_rotated")),
        "renewed30d": counts_since(account_id, "renewed", base_dir=base_dir),
        "deviceRefreshed30d": counts_since(account_id, "device_refreshed", base_dir=base_dir),
        "expired30d": counts_since(account_id, "expired", base_dir=base_dir),
        "error": row.get("error") or "",
        "warningAt": (warning or {}).get("touched_at") or "",
    }


# ------------------------------------------------------- sid_guard 等窗口解析

def parse_sid_guard(value: str) -> dict[str, Any] | None:
    """解析抖音 ``sid_guard``：``<sessionid>|<签发秒>|<有效秒>|<到期HTTP日期>``。

    返回 ``{"issuedAt", "expiresAt", "validSeconds", "validDays"}``（本地时间字符串）。
    解析失败返回 ``None``——绝不让格式变化打断巡检。
    """
    text = unquote(str(value or "").strip())
    parts = text.split("|")
    if len(parts) < 3:
        return None
    try:
        issued = datetime.fromtimestamp(int(parts[1]))
        valid_seconds = int(parts[2])
    except (ValueError, OSError, OverflowError):
        return None
    if valid_seconds <= 0:
        return None
    expires = issued + timedelta(seconds=valid_seconds)
    return {
        "issuedAt": now_text(issued),
        "expiresAt": now_text(expires),
        "validSeconds": valid_seconds,
        "validDays": round(valid_seconds / 86400, 2),
    }


def sid_guard_from_cookies(cookies: Iterable[dict]) -> dict[str, Any] | None:
    """从 Playwright cookie 列表里取 ``sid_guard`` 并解析窗口。"""
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        if str(cookie.get("name") or "") == "sid_guard":
            parsed = parse_sid_guard(str(cookie.get("value") or ""))
            if parsed:
                return parsed
    return None


def expiry_from_cookies(cookies: Iterable[dict], names: Iterable[str]) -> dict[str, Any] | None:
    """没有 ``sid_guard`` 的平台：用会话 cookie 自带的到期时间兜底。"""
    wanted = {str(name) for name in names}
    farthest = None
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        if str(cookie.get("name") or "") not in wanted:
            continue
        try:
            expires = float(cookie.get("expires") or 0)
        except (TypeError, ValueError):
            continue
        if expires <= time.time():
            continue
        moment = datetime.fromtimestamp(expires)
        if farthest is None or moment > farthest:
            farthest = moment
    if farthest is None:
        return None
    return {"expiresAt": now_text(farthest), "issuedAt": "", "validSeconds": 0, "validDays": 0.0}


def reset_for_tests() -> None:
    with SCHEMA_LOCK:
        _INITIALIZED.clear()


def _selftest() -> None:  # pragma: no cover - 手工排查用
    parsed = parse_sid_guard("abc%7C1795158142%7C5184000%7CTue,+19-Jan-2027+07:02:22+GMT")
    print("parse_sid_guard:", parsed)
    print("db:", os.fspath(db_path()))


if __name__ == "__main__":  # pragma: no cover
    _selftest()
