# -*- coding: utf-8 -*-
"""Persistent account automation pause used by every automatic platform path."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from loguru import logger


RISK_KEYWORDS = (
    "风控", "访问频繁", "操作频繁", "请求频繁", "账号受限", "流量限制",
    "商业权益", "安全验证", "异常验证", "违规", "处罚", "封禁", "封号",
    "环境异常", "风险控制", "账号异常", "不规范", "浏览行为异常",
    "第三方工具", "模拟浏览",
)
RESTRICTION_KEYWORDS = (
    "账号受限", "账号异常", "流量限制", "限制流量", "商业权益",
    "违规", "处罚", "封禁", "封号", "不规范", "浏览行为异常",
    "第三方工具", "模拟浏览",
)
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _value(account: Any, key: str, default: Any = "") -> Any:
    if isinstance(account, dict):
        return account.get(key, default)
    try:
        return account[key]
    except (KeyError, IndexError, TypeError):
        return default


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _normalize_until(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = _parse_time(text)
    if parsed is None:
        raise ValueError("暂停截止时间格式不正确")
    return parsed.strftime(_TIME_FORMAT)


def automation_state(account: Any) -> dict[str, Any]:
    """Return the effective automation state, expiring timed pauses lazily."""
    automation = _value(account, "automation", None)
    if isinstance(automation, dict):
        paused = bool(automation.get("paused"))
        until = str(automation.get("pauseUntil") or "")
        reason = str(automation.get("reason") or "")
        updated = str(automation.get("updatedAt") or "")
    else:
        try:
            paused = bool(int(_value(account, "automation_paused", 0) or 0))
        except (TypeError, ValueError):
            paused = False
        until = str(_value(account, "pause_until", "") or "")
        reason = str(_value(account, "pause_reason", "") or "")
        updated = str(_value(account, "pause_updated_at", "") or "")
    expiry = _parse_time(until)
    if paused and expiry is not None and expiry <= datetime.now():
        paused = False
    return {
        "configured": True,
        "paused": paused,
        "pauseUntil": until,
        "reason": reason,
        "updatedAt": updated,
    }


def is_paused(account: Any) -> bool:
    return bool(automation_state(account).get("paused"))


def filter_active_accounts(accounts: Iterable[Any]) -> list[Any]:
    return [account for account in accounts or [] if not is_paused(account)]


def _base_dir(value: str | Path | None) -> Path:
    if value is not None:
        return Path(value)
    from conf import BASE_DIR

    return Path(BASE_DIR)


def ensure_schema(base_dir: str | Path | None = None) -> bool:
    """Apply the additive account schema before reading pause columns."""
    from account_store import get_store

    return bool(get_store(_base_dir(base_dir)).ensure_schema())


def _account_row(account_id: int, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    from account_store import get_store

    return get_store(_base_dir(base_dir)).get(int(account_id))


def is_account_paused(account_id: int, base_dir: str | Path | None = None) -> bool:
    item = _account_row(account_id, base_dir)
    return bool(item and is_paused(item))


def is_named_account_paused(
    account_name: str,
    platform_label: str = "",
    base_dir: str | Path | None = None,
) -> bool:
    """Check pause state for integrations that persist only a display name."""
    from account_store import platform_type_of
    from db_utils import connect

    name = str(account_name or "").strip()
    if not name:
        return False
    if not ensure_schema(base_dir):
        raise RuntimeError("账号表结构初始化失败，无法确认自动动作状态")
    db_path = _base_dir(base_dir) / "db" / "database.db"
    if not db_path.is_file():
        return False
    params: list[Any] = [name]
    sql = (
        "SELECT automation_paused,pause_until,pause_reason,pause_updated_at "
        "FROM user_info WHERE userName=?"
    )
    platform_type = platform_type_of(platform_label) if platform_label else None
    if platform_type is not None:
        sql += " AND type=?"
        params.append(platform_type)
    sql += " LIMIT 1"
    try:
        with connect(db_path, timeout=10) as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
    except Exception:
        return False
    return bool(row and is_paused(dict(row)))


def pause_named_on_risk(
    account_name: str,
    platform_label: str,
    message: str,
    base_dir: str | Path | None = None,
) -> bool:
    """Pause a display-name integration when a platform risk signal is known."""
    from account_store import platform_type_of
    from db_utils import connect

    name = str(account_name or "").strip()
    if not name or not any(keyword in str(message or "") for keyword in RISK_KEYWORDS):
        return False
    if not ensure_schema(base_dir):
        return False
    params: list[Any] = [name]
    sql = "SELECT id FROM user_info WHERE userName=?"
    platform_type = platform_type_of(platform_label) if platform_label else None
    if platform_type is not None:
        sql += " AND type=?"
        params.append(platform_type)
    sql += " LIMIT 1"
    try:
        with connect(_base_dir(base_dir) / "db" / "database.db", timeout=10) as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
    except Exception:
        return False
    return bool(row and pause_on_risk(int(row["id"]), message, base_dir=base_dir))


def pause_account(
    account_id: int,
    reason: str,
    until: str = "",
    actor: str = "",
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    from account_store import get_store
    from db_utils import connect

    store = get_store(_base_dir(base_dir))
    if not store.ensure_schema():
        raise RuntimeError("账号表结构初始化失败，不能写入暂停状态")
    until_text = _normalize_until(until)
    reason_text = str(reason or "人工暂停自动动作").strip()[:300]
    now = datetime.now().strftime(_TIME_FORMAT)
    with connect(store.db_path, timeout=15) as conn:
        cursor = conn.execute(
            "UPDATE user_info SET automation_paused=1, pause_until=?, "
            "pause_reason=?, pause_updated_at=? WHERE id=?",
            (until_text, reason_text, now, int(account_id)),
        )
        if not cursor.rowcount:
            return None
    logger.warning(
        "账号自动动作已暂停 id={} actor={} until={} reason={}",
        account_id, actor or "system", until_text or "manual-resume", reason_text,
    )
    return store.get(int(account_id))


def resume_account(
    account_id: int,
    actor: str = "",
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    from account_store import get_store
    from db_utils import connect

    store = get_store(_base_dir(base_dir))
    if not store.ensure_schema():
        raise RuntimeError("账号表结构初始化失败，不能恢复自动动作")
    now = datetime.now().strftime(_TIME_FORMAT)
    with connect(store.db_path, timeout=15) as conn:
        cursor = conn.execute(
            "UPDATE user_info SET automation_paused=0, pause_until='', "
            "pause_reason='', pause_updated_at=? WHERE id=?",
            (now, int(account_id)),
        )
        if not cursor.rowcount:
            return None
    logger.info("账号自动动作已恢复 id={} actor={}", account_id, actor or "system")
    return store.get(int(account_id))


def pause_on_risk(
    account_id: int,
    message: str,
    *,
    base_dir: str | Path | None = None,
) -> bool:
    """Persist a pause only for high-confidence platform restriction signals."""
    text = str(message or "")
    if not any(keyword in text for keyword in RISK_KEYWORDS):
        return False
    configured = str(os.environ.get("FLOWOPS_ACCOUNT_RISK_PAUSE_SECONDS", "") or "").strip()
    if configured:
        try:
            seconds = int(configured)
        except ValueError:
            seconds = 1800
    elif any(keyword in text for keyword in RESTRICTION_KEYWORDS):
        # Restriction notices commonly cover days. Keep the account quiet until
        # an operator has reviewed the platform result instead of retrying soon.
        seconds = 7 * 24 * 3600
    else:
        seconds = 1800
    seconds = max(60, min(seconds, 7 * 24 * 3600))
    proposed_until = datetime.now() + timedelta(seconds=seconds)
    current = _account_row(account_id, base_dir)
    if current:
        state = automation_state(current)
        current_until = _parse_time(state.get("pauseUntil"))
        if state.get("paused") and (
            current_until is None or current_until >= proposed_until
        ):
            logger.info(
                "账号已有更长暂停，保留原观察期 id={} until={}",
                account_id, state.get("pauseUntil") or "manual-resume",
            )
            return True
    paused = pause_account(
        account_id,
        f"平台风险信号，自动暂停：{text[:220]}",
        until=proposed_until.strftime(_TIME_FORMAT),
        actor="risk-detector",
        base_dir=base_dir,
    )
    return paused is not None
