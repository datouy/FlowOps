# -*- coding: utf-8 -*-
"""共享的平台私信账号解析与通用类型转换。"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from conf import BASE_DIR

ACCOUNT_DB = Path(BASE_DIR) / "db" / "database.db"
COOKIE_DIR = Path(BASE_DIR) / "cookiesFile"


class SyncError(RuntimeError):
    """可直接展示给用户的同步错误。"""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def as_flag(value: Any) -> int:
    return 1 if value in (True, 1, "1", "true", "on", "yes") else 0


def clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\u200b", "").replace("\ufeff", "").strip()
    return text[:limit]


def parse_account_ids(value: Any) -> list[int]:
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        text = str(value or "").strip()
        if not text:
            raw = []
        else:
            try:
                parsed = json.loads(text)
                raw = parsed if isinstance(parsed, list) else re.split(r"[,，\s]+", text)
            except (TypeError, ValueError):
                raw = re.split(r"[,，\s]+", text)
    result: list[int] = []
    for item in raw:
        try:
            account_id = int(item)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in result:
            result.append(account_id)
    return result


def cookie_path(value: Any) -> Path | None:
    name = str(value or "").strip()
    if not name:
        return None
    path = COOKIE_DIR / Path(name).name
    if path.is_file() and path.stat().st_size > 2:
        return path
    return None


def account_rows(account_type: int) -> list[dict]:
    if not ACCOUNT_DB.exists():
        return []
    with sqlite3.connect(ACCOUNT_DB, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            "SELECT id,type,userName,filePath,status FROM user_info "
            "WHERE type=? ORDER BY id", (int(account_type),),
        ).fetchall()]
    for row in rows:
        path = cookie_path(row.get("filePath"))
        row["cookie_path"] = str(path) if path else ""
        row["has_cookie"] = bool(path)
    return rows


def account_name(account: dict | None) -> str:
    if not account:
        return ""
    return clean_text(account.get("userName"), 120) or f"账号{account.get('id') or ''}"


def public_account(account: dict | None) -> dict | None:
    if not account:
        return None
    return {
        "id": int(account.get("id") or 0),
        "name": account_name(account),
        "type": int(account.get("type") or 0),
        "status": int(account.get("status") or 0),
        "has_cookie": bool(account.get("cookie_path")),
        "cookie_file": Path(str(account.get("cookie_path"))).name if account.get("cookie_path") else "",
    }


def resolve_accounts(accounts: list[dict], selected_ids: list[int]) -> list[dict]:
    if selected_ids:
        selected = [
            item for item in accounts if int(item.get("id") or 0) in selected_ids
        ]
    else:
        selected = [item for item in accounts if item.get("cookie_path")]
        if not selected:
            selected = list(accounts)
    return selected


def validate_account_ids(accounts: list[dict], selected_ids: list[int]) -> list[int]:
    known = {int(item.get("id") or 0) for item in accounts}
    return [item for item in selected_ids if item not in known]


def normalize_timestamp(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        try:
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            numeric = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            seconds = numeric / 1000.0 if numeric > 10_000_000_000 else numeric
            try:
                parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def timestamp_text(value: Any) -> str:
    parsed = normalize_timestamp(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else now_text()


def is_recent(value: Any, hours: int = 48) -> bool:
    parsed = normalize_timestamp(value)
    if parsed is None:
        return True
    return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() <= hours * 3600
