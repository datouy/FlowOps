# -*- coding: utf-8 -*-
"""共享的平台私信账号解析与通用类型转换。"""
from __future__ import annotations

import inspect
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request
from loguru import logger

from conf import BASE_DIR
import dm_cursor
import dm_ops
import runtime_guard
from db_utils import apply_migrations, connect
from utils.credential_crypto import resolve_credential_path

ACCOUNT_DB = Path(BASE_DIR) / "db" / "database.db"
COOKIE_DIR = Path(BASE_DIR) / "cookiesFile"

# 历史回补窗口：默认 7 天，前端可调；0 表示不按时间过滤（尽量把平台给的全收进来）。
DEFAULT_HISTORY_DAYS = 7
MIN_HISTORY_DAYS = 0
MAX_HISTORY_DAYS = 30


def clamp_history_days(value: Any, default: int = DEFAULT_HISTORY_DAYS) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_HISTORY_DAYS, min(days, MAX_HISTORY_DAYS))


def history_window_hours(settings: dict | None, default_days: int = DEFAULT_HISTORY_DAYS) -> int:
    """按设置算出同步窗口（小时）。``recent_only`` 关掉时返回 0 表示不过滤。"""
    data = settings or {}
    if not as_flag(data.get("recent_only", 1)):
        return 0
    days = clamp_history_days(data.get("history_days", default_days), default_days)
    return days * 24


class SyncError(RuntimeError):
    """可直接展示给用户的同步错误。"""


# ---- 同步错误归因 ------------------------------------------------------
# 失败原因决定用户该做什么：账号失效 → 去重扫；没登录账号 → 去添加；
# Cookie 文件丢了 → 重扫；网络 → 等下一轮。前端按分类给提示。
DM_ERROR_ACCOUNT_EXPIRED = "account_expired"
DM_ERROR_NO_ACCOUNTS = "no_accounts"
DM_ERROR_COOKIE_MISSING = "cookie_missing"
DM_ERROR_NETWORK = "network"
DM_ERROR_UNKNOWN = "error"

_DM_ERROR_RULES = (
    # 先判 Cookie 缺失：这类消息里通常也带"请重新扫码"，会被误判成账号失效
    (
        DM_ERROR_COOKIE_MISSING,
        ("Cookie 文件不存在", "Cookie 文件无法读取", "Cookie 文件为空"),
    ),
    (
        DM_ERROR_ACCOUNT_EXPIRED,
        ("300334", "300333", "300336", "登录已失效", "登录态失效", "重新扫码", "expired"),
    ),
    (
        DM_ERROR_NETWORK,
        ("连接视频号失败", "连接失败", "Timeout", "超时", "Connection", "ECONN", "SSL"),
    ),
)


def classify_dm_error(message: str) -> str:
    """把同步错误翻译成前端可行动的类别；空字符串表示没出错。"""
    text = str(message or "")
    if not text:
        return ""
    if "未找到" in text and "账号" in text:
        return DM_ERROR_NO_ACCOUNTS
    lowered = text.lower()
    for kind, keywords in _DM_ERROR_RULES:
        for keyword in keywords:
            if keyword.lower() in lowered:
                return kind
    return DM_ERROR_UNKNOWN


def friendly_error(exc: Exception) -> str:
    """把底层异常转换成适合展示给用户的中文提示。"""
    text = str(exc).strip() or exc.__class__.__name__
    lower = text.lower()
    if "target page, context or browser has been closed" in lower:
        if "xserver" in lower or "x server" in lower or "$display" in lower:
            return "服务器没有图形界面，请在无头模式或 Xvfb 环境中启动浏览器"
        return "平台页面意外关闭，请稍后重试"
    if "executable doesn't exist" in lower or "browsertype.launch" in lower:
        return "Playwright 浏览器未安装，请执行 playwright install chromium"
    return text[:500]


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
    """账号表里记的逻辑凭据名 → 磁盘上实际存在的文件。

    登录态迁移成密文后，磁盘上是 ``<32hex>.enc``，账号表里还是
    ``<32hex>.json``。这里必须走 ``resolve_credential_path``（密文优先），
    直接 ``Path("x.json").is_file()`` 会把正常账号判成"Cookie 文件不存在"，
    抖音/小红书/视频号同步全部报 cookie_missing。
    """
    name = str(value or "").strip()
    if not name:
        return None
    path = COOKIE_DIR / Path(name).name
    resolved = resolve_credential_path(path)
    if resolved is None:
        return None
    try:
        if resolved.stat().st_size <= 2:
            return None
    except OSError:
        return None
    return resolved


def account_rows(account_type: int) -> list[dict]:
    if not ACCOUNT_DB.exists():
        return []
    with connect(ACCOUNT_DB, timeout=15) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT id,type,userName,filePath,status FROM user_info "
            "WHERE type=? ORDER BY id", (int(account_type),),
        ).fetchall()]
    for row in rows:
        path = cookie_path(row.get("filePath"))
        row["cookie_path"] = str(path) if path else ""
        row["has_cookie"] = bool(path)
    return rows


def account_name(account: dict | None, fallback_prefix: str = "账号") -> str:
    if not account:
        return ""
    name = clean_text(account.get("userName") or account.get("name"), 120)
    if name:
        return name
    account_id = int(account.get("id") or 0)
    return f"{fallback_prefix}{account_id}" if account_id else fallback_prefix


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


def _dm_account_alive(account: dict | None) -> bool:
    """私信同步口径的存活判断：只有 status == 0 才算失效。

    status 缺失（老数据）或 -1（验证中）都视为存活，避免巡检抖动时
    同步目标来回跳。与 stats_reflow._is_alive_account 保持一致。
    """
    if not account:
        return False
    if account.get("status") is None:
        return True
    try:
        return int(account.get("status")) != 0
    except (TypeError, ValueError):
        return True


def resolve_accounts(accounts: list[dict], selected_ids: list[int]) -> list[dict]:
    if selected_ids:
        selected = [
            item for item in accounts if int(item.get("id") or 0) in selected_ids
        ]
    else:
        selected = [item for item in accounts if item.get("cookie_path")]
        if not selected:
            selected = list(accounts)
    # 失效账号全局隐藏：即使被显式选中也不再同步。
    return [item for item in selected if _dm_account_alive(item)]


def validate_account_ids(accounts: list[dict], selected_ids: list[int]) -> list[int]:
    known = {int(item.get("id") or 0) for item in accounts}
    return [item for item in selected_ids if item not in known]


def _out_of_scope_account_ids(selected_ids: list[int]) -> list[int]:
    """返回"存在但不在令牌范围内"的账号 id。

    ``list_accounts`` 已经按范围过滤过了，所以这里的"缺失"有两种可能：
    账号真被删了，或者账号存在但属于别的运营。这里把后者挑出来，
    便于接口返回 403 而不是含糊的 400。
    """
    try:
        import access_scope
    except Exception:
        return []
    try:
        if access_scope.unrestricted():
            return []
    except Exception:
        return []
    out: list[int] = []
    for account_id in selected_ids or []:
        try:
            value = int(account_id)
        except (TypeError, ValueError):
            continue
        if access_scope.account_in_scope(account_id=value):
            continue
        # 账号真的不存在时不算越权。
        if not access_scope.account_exists(value):
            continue
        out.append(value)
    return out


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


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


class BaseDmSync:
    """平台私信同步的公共生命周期。

    子类只需实现 ``scan_account``（可选支持 ``quick`` 增量模式）以及少量平台
    特有钩子。设置持久化、账号选择、状态聚合、多账号并行、持续轮询（间隔为
    0 时上一轮结束立即开始下一轮）、失败退避和三个 HTTP 路由都由这里统一处理。
    """

    def __init__(
        self,
        *,
        blueprint: Blueprint,
        key: str,
        platform: str,
        account_type: int,
        settings_table: str,
        route_prefix: str,
        min_interval: int,
        default_interval: int,
        default_max_conversations: int,
        max_conversations: int = 100,
        max_conversations_load: int | None = None,
        max_interval: int = 3600,
        recent_hours: int = 48,
        default_history_days: int = DEFAULT_HISTORY_DAYS,
        auto_trigger: str = "timer",
        initial_delay: float = 5,
        prefer_active_accounts: bool = False,
        error_types: tuple[type[BaseException], ...] = (),
        max_workers: int = 8,
        failure_backoff: float = 30.0,
        continuous_gap: float = 0.5,
        no_accounts_backoff: float = 1800.0,
        quick_triggers: tuple[str, ...] = ("auto",),
    ):
        self.blueprint = blueprint
        self.key = key
        self.platform = platform
        self.account_type = int(account_type)
        self.settings_table = settings_table
        self.route_prefix = route_prefix.rstrip("/")
        # 0 表示持续轮询：一轮结束后立刻开始下一轮。
        self.min_interval = max(0, int(min_interval))
        self.default_interval = max(self.min_interval, int(default_interval))
        self.max_interval = max(self.default_interval, int(max_interval))
        self.default_max_conversations = max(1, int(default_max_conversations))
        self.max_conversations = max(self.default_max_conversations, int(max_conversations))
        self.max_conversations_load = max(
            self.max_conversations,
            int(max_conversations_load or self.max_conversations),
        )
        self.recent_hours = max(1, int(recent_hours))
        self.default_history_days = clamp_history_days(default_history_days)
        self.auto_trigger = auto_trigger
        self.initial_delay = max(0.0, float(initial_delay))
        self.prefer_active_accounts = prefer_active_accounts
        self.error_types = (SyncError, *error_types)
        self.max_workers = max(1, int(max_workers))
        self.failure_backoff = max(1.0, float(failure_backoff))
        self.continuous_gap = max(0.0, float(continuous_gap))
        # 没有账号时的重试间隔：默认 30 分钟。
        # 旧代码对"未找到账号"和真实错误一视同仁，每 30 秒重试一次，
        # 日志被刷满、后台线程一直在空转。
        self.no_accounts_backoff = max(self.failure_backoff, float(no_accounts_backoff))
        self.quick_triggers = tuple(quick_triggers or ())
        self._scan_accepts_quick = self._detect_quick_support()
        self.log = logger.bind(dm_sync=key)

        self.default_settings = {
            "enabled": 1,
            "account_id": 0,
            "account_ids": [],
            "interval_seconds": self.default_interval,
            "max_conversations": self.default_max_conversations,
            "recent_only": 1,
            "history_days": self.default_history_days,
            "draft_only": 1,
        }
        self.sync_fields = {
            "enabled", "account_id", "account_ids", "interval_seconds",
            "max_conversations", "recent_only", "history_days", "draft_only",
            "last_started_at", "last_finished_at", "last_success_at", "last_error",
            "last_scanned", "last_imported", "last_duplicates", "last_skipped",
            "last_failed", "last_duration_ms", "last_account_name",
            "last_cookie_file", "last_run_trigger", "last_skip_reasons",
        }

        self._sync_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._worker_started = False
        self._next_run_at: float | None = None

    # ---- 平台钩子 -----------------------------------------------------
    def scan_account(self, settings: dict, account: dict, quick: bool = False) -> dict:
        """扫描单个账号；支持 ``quick`` 的子类可做低延迟增量扫描。"""
        raise NotImplementedError

    def _detect_quick_support(self) -> bool:
        try:
            params = inspect.signature(self.scan_account).parameters
        except (TypeError, ValueError):
            return False
        return "quick" in params

    def init_extra(self, conn: sqlite3.Connection) -> None:
        """创建平台特有的表或执行一次性迁移。"""

    def reset_history_state(self) -> None:
        """历史窗口变大后清掉平台侧的“已同步”标记，让下一轮重新回补历史。"""

    def extra_migrations(self) -> dict:
        """平台可追加迁移；版本号从 3 开始，避免和公共迁移冲突。"""
        return {}

    def friendly_error(self, exc: Exception) -> str:
        return friendly_error(exc)

    def no_accounts_message(self) -> str:
        return f"未找到{self.platform}账号，请先在账号管理中登录"

    def no_accounts_is_normal(self) -> bool:
        """没有可用账号算不算"正常状态"。

        没登录任何账号时，后台定时同步每一轮都会失败。这不是错误，
        而是"还没配置"的正常状态——用户还没扫码登录而已。
        默认当作正常状态，日志降级为 INFO，并且拉长重试间隔，
        免得日志被刷屏、CPU 空转。
        """
        return True

    def account_name(self, account: dict | None) -> str:
        return account_name(account, self.platform)

    # ---- 设置与账号 ---------------------------------------------------
    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {self.settings_table} (
            id INTEGER PRIMARY KEY CHECK (id=1),
            enabled INTEGER DEFAULT 1,
            account_id INTEGER DEFAULT 0,
            account_ids TEXT DEFAULT '[]',
            interval_seconds INTEGER DEFAULT 60,
            max_conversations INTEGER DEFAULT 50,
            recent_only INTEGER DEFAULT 1,
            history_days INTEGER DEFAULT 7,
            draft_only INTEGER DEFAULT 1,
            last_started_at TEXT DEFAULT '',
            last_finished_at TEXT DEFAULT '',
            last_success_at TEXT DEFAULT '',
            last_error TEXT DEFAULT '',
            last_scanned INTEGER DEFAULT 0,
            last_imported INTEGER DEFAULT 0,
            last_duplicates INTEGER DEFAULT 0,
            last_skipped INTEGER DEFAULT 0,
            last_failed INTEGER DEFAULT 0,
            last_skip_reasons TEXT DEFAULT '{{}}',
            last_duration_ms INTEGER DEFAULT 0,
            last_account_name TEXT DEFAULT '',
            last_cookie_file TEXT DEFAULT '',
            last_run_trigger TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        for name, ddl in (
            ("account_id", "INTEGER DEFAULT 0"),
            ("account_ids", "TEXT DEFAULT '[]'"),
            ("draft_only", "INTEGER DEFAULT 1"),
            ("history_days", f"INTEGER DEFAULT {self.default_history_days}"),
            ("last_scanned", "INTEGER DEFAULT 0"),
            ("last_imported", "INTEGER DEFAULT 0"),
            ("last_duplicates", "INTEGER DEFAULT 0"),
            ("last_skipped", "INTEGER DEFAULT 0"),
            ("last_failed", "INTEGER DEFAULT 0"),
            ("last_duration_ms", "INTEGER DEFAULT 0"),
            ("last_skip_reasons", "TEXT DEFAULT '{}'"),
            ("last_account_name", "TEXT DEFAULT ''"),
            ("last_cookie_file", "TEXT DEFAULT ''"),
            ("last_run_trigger", "TEXT DEFAULT ''"),
        ):
            ensure_column(conn, self.settings_table, name, ddl)
        conn.execute(
            f"""INSERT OR IGNORE INTO {self.settings_table}
            (id,enabled,account_id,account_ids,interval_seconds,max_conversations,
             recent_only,history_days,draft_only)
            VALUES (1,?,?,?,?,?,?,?,?)""",
            (
                self.default_settings["enabled"],
                self.default_settings["account_id"],
                json.dumps(self.default_settings["account_ids"], ensure_ascii=False),
                self.default_settings["interval_seconds"],
                self.default_settings["max_conversations"],
                self.default_settings["recent_only"],
                self.default_settings["history_days"],
                self.default_settings["draft_only"],
            ),
        )
        self.init_extra(conn)
        # 账号级同步健康度：哪台账号最后一次成功同步是什么时候、失败原因是什么。
        # 汇总在 /dm/health，前端在私信中心顶部给一条"为什么没有新消息"的横幅。
        conn.execute("""CREATE TABLE IF NOT EXISTS dm_account_sync_state (
            platform TEXT NOT NULL,
            account_key TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT '',
            last_success_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            last_error_kind TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT NOT NULL DEFAULT '',
            last_imported INTEGER DEFAULT 0,
            PRIMARY KEY (platform, account_key))""")

    def _migrate_history_days(self, conn: sqlite3.Connection) -> None:
        """老库补 history_days 列：历史窗口从硬编码 48 小时改成可配置天数。"""
        ensure_column(
            conn, self.settings_table, "history_days",
            f"INTEGER DEFAULT {self.default_history_days}",
        )
        conn.execute(
            f"UPDATE {self.settings_table} SET history_days=? "
            "WHERE history_days IS NULL OR history_days<0",
            (self.default_history_days,),
        )

    def _ensure_health_table(self, conn: sqlite3.Connection) -> None:
        """幂等建表：老库的迁移版本已经跑完，新表不能只写在 v1 迁移里。"""
        conn.execute("""CREATE TABLE IF NOT EXISTS dm_account_sync_state (
            platform TEXT NOT NULL,
            account_key TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT '',
            last_success_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            last_error_kind TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT NOT NULL DEFAULT '',
            last_imported INTEGER DEFAULT 0,
            PRIMARY KEY (platform, account_key))""")

    def init_db(self) -> None:
        migrations = {1: self._create_schema, 2: self._migrate_history_days}
        migrations.update(self.extra_migrations())
        with dm_ops.get_conn() as conn:
            apply_migrations(
                conn,
                f"dm_sync:{self.settings_table}",
                migrations,
            )
            self._ensure_health_table(conn)
            # 建表兜底：迁移只在 version > 已记录版本时才跑。
            # 一旦库被换掉、恢复过备份或表被误删，版本号还在、表却没了，
            # 迁移就永远跳过——之后所有读写都是 "no such table"。
            # 所以这里无条件跑一次幂等的 CREATE TABLE IF NOT EXISTS，
            # 缺什么补什么，不缺就什么都不做。
            self._ensure_settings_table(conn)

    def _ensure_settings_table(self, conn: sqlite3.Connection) -> None:
        """幂等补齐设置表（表被删/换库后自愈）。

        ``_create_schema`` 里除了建表还会写默认行、跑平台特有的初始化，
        这里只做"表在不在"这一层，保证不会因为版本号对不上而缺表。
        """
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (self.settings_table,),
        ).fetchone()
        if row is not None:
            return
        self.log.warning("设置表 {} 缺失，重新创建", self.settings_table)
        # 平台特有的初始化会读 dm_meta（dm_ops 的基础表）。
        # 换库/恢复备份时那张表可能也没了，先把它补回来，
        # 否则建表过程自己先炸在 "no such table: dm_meta" 上。
        conn.execute("""CREATE TABLE IF NOT EXISTS dm_meta (
            key TEXT PRIMARY KEY, value TEXT DEFAULT '')""")
        self._create_schema(conn)

    def load_settings(self) -> dict:
        with dm_ops.get_conn() as conn:
            row = conn.execute(
                f"SELECT * FROM {self.settings_table} WHERE id=1"
            ).fetchone()
        data = dict(self.default_settings)
        if row:
            data.update(dict(row))
        data["account_ids"] = parse_account_ids(data.get("account_ids"))
        for key in ("enabled", "recent_only", "draft_only"):
            data[key] = as_flag(data.get(key, self.default_settings[key]))
        data["history_days"] = clamp_history_days(
            data.get("history_days", self.default_history_days), self.default_history_days,
        )
        data["history_window_hours"] = history_window_hours(
            data, self.default_history_days,
        )
        try:
            data["account_id"] = max(0, int(data.get("account_id") or 0))
        except (TypeError, ValueError):
            data["account_id"] = 0
        if not data["account_ids"] and data["account_id"]:
            data["account_ids"] = [data["account_id"]]
        try:
            raw_interval = data.get("interval_seconds")
            interval_value = int(
                self.default_interval if raw_interval is None else raw_interval
            )
        except (TypeError, ValueError):
            interval_value = self.default_interval
        data["interval_seconds"] = max(
            self.min_interval, min(interval_value, self.max_interval)
        )
        try:
            data["max_conversations"] = max(
                1,
                min(
                    int(data.get("max_conversations") or self.default_max_conversations),
                    self.max_conversations_load,
                ),
            )
        except (TypeError, ValueError):
            data["max_conversations"] = self.default_max_conversations
        try:
            data["last_skip_reasons"] = json.loads(data.get("last_skip_reasons") or "{}")
        except (TypeError, ValueError):
            data["last_skip_reasons"] = {}
        return data

    def save_settings(self, **fields) -> None:
        fields = {key: value for key, value in fields.items() if key in self.sync_fields}
        if not fields:
            return
        if "account_ids" in fields:
            fields["account_ids"] = json.dumps(
                parse_account_ids(fields["account_ids"]), ensure_ascii=False
            )
        fields["updated_at"] = now_text()
        assignments = ",".join(f"{key}=?" for key in fields)
        with dm_ops.get_conn() as conn:
            conn.execute(
                f"UPDATE {self.settings_table} SET {assignments} WHERE id=1",
                tuple(fields.values()),
            )

    def list_accounts(self) -> list[dict]:
        rows = account_rows(self.account_type)
        if self.prefer_active_accounts:
            rows.sort(key=lambda row: (
                0 if int(row.get("status") or 0) == 1 else 1,
                -int(row.get("id") or 0),
            ))
        # 多人协作：只暴露令牌范围内的账号。没有请求上下文（后台同步线程）
        # 时不过滤。
        try:
            import access_scope
            rows = access_scope.filter_accounts(rows)
        except Exception:
            pass
        return rows

    def resolve_accounts(self, accounts: list[dict], settings: dict) -> list[dict]:
        if not accounts:
            return []
        selected_ids = parse_account_ids(settings.get("account_ids"))
        if selected_ids:
            by_id = {int(item.get("id") or 0): item for item in accounts}
            selected = [
                by_id[account_id] for account_id in selected_ids if account_id in by_id
            ]
        else:
            selected = [item for item in accounts if item.get("cookie_path")]
            if self.prefer_active_accounts:
                active = [
                    item for item in selected if int(item.get("status") or 0) == 1
                ]
                if active:
                    selected = active
            if not selected:
                selected = list(accounts)
        # 失效账号全局隐藏：即使被显式选中也不再同步。
        selected = [item for item in selected if _dm_account_alive(item)]
        result = []
        for item in selected:
            prepared = dict(item)
            path = prepared.get("cookie_path") or cookie_path(prepared.get("filePath"))
            prepared["cookie_path"] = str(path) if path else ""
            result.append(prepared)
        return result

    @staticmethod
    def _cookie_display(account: dict) -> str:
        value = account.get("cookie_path") or account.get("filePath") or ""
        return Path(str(value)).name if value else ""

    # ---- 状态与路由 ---------------------------------------------------
    def next_run_text(self) -> str:
        with self._state_lock:
            next_at = self._next_run_at
        if not next_at:
            return ""
        return datetime.fromtimestamp(next_at).strftime("%Y-%m-%d %H:%M:%S")

    def status_payload(self) -> dict:
        settings = self.load_settings()
        accounts = self.list_accounts()
        selected = self.resolve_accounts(accounts, settings)
        data = dict(settings)
        data["platform"] = self.platform
        data["accounts"] = [public_account(item) for item in accounts]
        data["selected_accounts"] = [public_account(item) for item in selected]
        data["selected_account"] = public_account(selected[0]) if selected else None
        data["effective_account_ids"] = [int(item.get("id") or 0) for item in selected]
        data["account_count"] = len(selected)
        data["running"] = self._sync_lock.locked()
        data["next_run_at"] = self.next_run_text()
        data["history_days"] = int(settings.get("history_days") or self.default_history_days)
        data["history_window_hours"] = int(settings.get("history_window_hours") or 0)
        data["recent_window_hours"] = data["history_window_hours"]
        data["parallel_workers"] = min(self.max_workers, max(1, len(selected)))
        data["sync_mode"] = (
            "continuous" if int(settings.get("interval_seconds") or 0) <= 0 else "interval"
        )
        data["read_only"] = True
        # 前端据此区分"还没登录账号"（正常）和"同步真的出错了"（异常）。
        # 没有账号时不应该在页面上挂一个红色报错条。
        data["no_accounts"] = not selected
        data["last_error_is_account_hint"] = bool(
            not selected and self.no_accounts_is_normal()
        )
        # 分级概览：活跃/冷账号数量 + 夜间降频标记。
        data["tier_summary"] = dm_cursor.summary(self.platform)
        data["night_mode"] = dm_cursor.is_night()
        return data

    def register_routes(self) -> None:
        self.blueprint.add_url_rule(
            f"{self.route_prefix}/status",
            endpoint=f"{self.key}_sync_status",
            view_func=self.status_view,
            methods=["GET"],
        )
        self.blueprint.add_url_rule(
            f"{self.route_prefix}/settings",
            endpoint=f"{self.key}_sync_settings",
            view_func=self.settings_view,
            methods=["PUT"],
        )
        self.blueprint.add_url_rule(
            f"{self.route_prefix}/run",
            endpoint=f"{self.key}_sync_run",
            view_func=self.run_view,
            methods=["POST"],
        )

    def status_view(self):
        return jsonify({"code": 200, "data": self.status_payload(), "msg": None})

    def settings_view(self):
        data = request.get_json(silent=True) or {}
        try:
            raw_interval = data.get("interval_seconds")
            interval_value = int(
                self.default_interval if raw_interval is None else raw_interval
            )
            interval = max(self.min_interval, min(interval_value, self.max_interval))
            max_conversations = max(
                1,
                min(
                    int(data.get("max_conversations") or self.default_max_conversations),
                    self.max_conversations,
                ),
            )
        except (TypeError, ValueError):
            return jsonify({"code": 400, "msg": "同步间隔或会话数量格式不正确"}), 400
        if "account_ids" in data:
            account_ids = parse_account_ids(data.get("account_ids"))
        else:
            try:
                legacy_id = max(0, int(data.get("account_id") or 0))
            except (TypeError, ValueError):
                return jsonify({"code": 400, "msg": f"{self.platform}账号格式不正确"}), 400
            account_ids = [legacy_id] if legacy_id else []
        accounts = self.list_accounts()
        missing = validate_account_ids(accounts, account_ids)
        if missing:
            # 越权选别人范围的账号时返回 403（而不是混在 400 里），
            # 让"越权"和"账号不存在"在日志与前端上可区分。
            out_of_scope = _out_of_scope_account_ids(account_ids)
            if out_of_scope:
                return jsonify({
                    "code": 403,
                    "msg": f"无权操作{self.platform}账号：" + "、".join(str(x) for x in out_of_scope),
                }), 403
            return jsonify({
                "code": 400, "msg": f"所选{self.platform}账号不存在或已删除",
            }), 400

        enabled = as_flag(data.get("enabled", 1))
        recent_only = as_flag(data.get("recent_only", 1))
        history_days = clamp_history_days(
            data.get("history_days", self.default_history_days),
            self.default_history_days,
        )
        previous = self.load_settings()
        window_changed = (
            int(previous.get("history_days") or 0) != int(history_days)
            or bool(previous.get("recent_only")) != bool(recent_only)
        )
        self.save_settings(
            enabled=enabled,
            account_id=account_ids[0] if len(account_ids) == 1 else 0,
            account_ids=account_ids,
            interval_seconds=interval,
            max_conversations=max_conversations,
            recent_only=recent_only,
            history_days=history_days,
            draft_only=1,
        )
        if window_changed:
            # 窗口变大后必须让下一轮重新回补，否则旧的“已同步”标记会挡住历史。
            self.reset_history_state()
        with self._state_lock:
            # 保存后立即触发一轮，持续模式下由 finally 继续排下一轮。
            self._next_run_at = time.time() if enabled else None
        self._wake_event.set()
        return jsonify({
            "code": 200, "data": self.status_payload(), "msg": "同步设置已保存",
        })

    def run_view(self):
        result = self.run_sync("manual", wait_seconds=60)
        if not result.get("ok"):
            status = 409 if result.get("busy") else 400
            # 错误原因不能是空串——前端会直接把它显示给用户，
            # 兜底成"同步失败"等于什么都没说。
            message = str(result.get("error") or "").strip()
            if not message:
                message = (
                    "本轮没有账号被执行（分级调度已推迟），稍后会自动重试"
                    if result.get("skipped_all")
                    else "同步未完成，请稍后重试"
                )
            return jsonify({
                "code": status,
                "data": result,
                "msg": message,
            }), status
        return jsonify({"code": 200, "data": result, "msg": "同步完成"})

    # ---- 同步执行 -----------------------------------------------------
    def _failed_account_result(self, account: dict, message: str) -> dict:
        return {
            "ok": False,
            "account": public_account(account),
            "error": message,
            "scanned": 0,
            "imported": 0,
            "duplicates": 0,
            "skipped": 0,
            "failed": 1,
            "auto_replies_ready": 0,
            "skip_reasons": {},
            "items": [],
        }

    def _scan_one(self, settings: dict, account: dict, quick: bool) -> dict:
        try:
            if self._scan_accepts_quick:
                return self.scan_account(settings, account, quick=quick)
            return self.scan_account(settings, account)
        except Exception as exc:
            message = (
                str(exc) if isinstance(exc, self.error_types)
                else self.friendly_error(exc)
            )
            return self._failed_account_result(account, message)

    def _account_key(self, account: dict) -> str:
        return str(account.get("id") or self.account_name(account) or "")[:64]

    def _scan_accounts(
        self, settings: dict, accounts: list[dict], quick: bool = False,
    ) -> list[dict]:
        """按账号并行扫描；结果顺序与账号顺序一致。

        冷账号按分级调度跳过：本轮还没到它的执行时间就不扫描，
        避免每个账号都用同样的高频轮询。
        """
        due_accounts = []
        skipped_cold = 0
        for account in accounts:
            try:
                if dm_cursor.is_due(self.platform, self._account_key(account)):
                    due_accounts.append(account)
                else:
                    skipped_cold += 1
            except Exception:
                due_accounts.append(account)
        self._skipped_cold = skipped_cold
        if not due_accounts:
            return []
        if len(due_accounts) <= 1 or self.max_workers <= 1:
            results = [self._scan_one(settings, account, quick) for account in due_accounts]
        else:
            workers = max(1, min(self.max_workers, len(due_accounts)))
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix=f"{self.key}-account",
            ) as pool:
                futures = [
                    pool.submit(self._scan_one, settings, account, quick)
                    for account in due_accounts
                ]
                results = [future.result() for future in futures]
        for result in results:
            self._record_account_state(result)
            self._update_account_cursor(result)
        return results

    def _update_account_cursor(self, result: dict) -> None:
        """保存账号游标并更新分级（活跃/冷）与下次执行时间。"""
        account = result.get("account") or {}
        key = self._account_key(account)
        if not key:
            return
        try:
            if result.get("ok"):
                cursor = dm_cursor.decode_cursor(result.get("cursor"))
                if not cursor:
                    cursor = dm_cursor.cursor_from_items(result.get("items") or [])
                existing = dm_cursor.load_cursor(self.platform, key)
                dm_cursor.save_cursor(
                    self.platform, key,
                    last_message_id=cursor.get("lastMessageId")
                    or existing.get("lastMessageId") or "",
                    last_message_at=cursor.get("lastMessageAt")
                    or existing.get("lastMessageAt") or "",
                    message_count=int(
                        cursor.get("messageCount") or existing.get("messageCount") or 0
                    ),
                )
            dm_cursor.update_after_sync(
                self.platform, key,
                ok=bool(result.get("ok")),
                new_messages=int(result.get("imported") or 0),
            )
        except Exception as exc:
            logger.warning("更新私信游标失败: {}", exc)

    def _record_account_state(self, result: dict) -> None:
        """把每个账号本轮同步结果落库（健康度/最后成功时间/失败原因）。"""
        account = result.get("account") or {}
        key = str(account.get("id") or account.get("name") or "")[:64]
        if not key:
            return
        now = now_text()
        ok = bool(result.get("ok"))
        error = "" if ok else str(result.get("error") or "同步失败")[:500]
        kind = "" if ok else classify_dm_error(error)
        try:
            with dm_ops.get_conn() as conn:
                conn.execute(
                    """INSERT INTO dm_account_sync_state
                       (platform, account_key, account_name, last_success_at,
                        last_error, last_error_kind, last_checked_at, last_imported)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(platform, account_key) DO UPDATE SET
                         account_name=excluded.account_name,
                         last_success_at=CASE WHEN excluded.last_success_at != ''
                                              THEN excluded.last_success_at
                                              ELSE dm_account_sync_state.last_success_at END,
                         last_error=excluded.last_error,
                         last_error_kind=excluded.last_error_kind,
                         last_checked_at=excluded.last_checked_at,
                         last_imported=excluded.last_imported""",
                    (
                        self.platform, key, self.account_name(account),
                        now if ok else "", error, kind, now,
                        int(result.get("imported") or 0),
                    ),
                )
        except Exception as exc:
            self.log.warning("记录账号同步健康度失败: {}", exc)

    def health_payload(self) -> dict:
        """同步健康度：给"为什么没有新消息"一个可读的答案。

        平台级 + 账号级两层：平台级看最后一次成功时间与错误分类，
        账号级看每个账号各自的状态（有些号好好的、个别号失效是最常见的情况）。
        """
        settings = self.load_settings()
        accounts = self.resolve_accounts(self.list_accounts(), settings)
        states: dict[str, dict] = {}
        try:
            with dm_ops.get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM dm_account_sync_state WHERE platform=?",
                    (self.platform,),
                ).fetchall()
            states = {str(row["account_key"]): dict(row) for row in rows}
        except Exception:
            states = {}

        now = datetime.now()
        items = []
        for account in accounts:
            key = str(account.get("id") or account.get("name") or "")[:64]
            state = states.get(key) or {}
            last_success = str(state.get("last_success_at") or "")
            stale_hours = None
            if last_success:
                try:
                    stale_hours = round(
                        (now - datetime.strptime(last_success, "%Y-%m-%d %H:%M:%S"))
                        .total_seconds() / 3600,
                        1,
                    )
                except ValueError:
                    stale_hours = None
            items.append({
                "id": account.get("id"),
                "name": self.account_name(account),
                "lastSuccessAt": last_success,
                "lastError": str(state.get("last_error") or ""),
                "errorKind": str(state.get("last_error_kind") or ""),
                "staleHours": stale_hours,
                "lastImported": int(state.get("last_imported") or 0),
            })

        last_error = str(settings.get("last_error") or "")
        return {
            "platform": self.platform,
            "key": self.key,
            "enabled": bool(settings.get("enabled")),
            "running": self._sync_lock.locked(),
            "lastSuccessAt": str(settings.get("last_success_at") or ""),
            "lastError": last_error,
            "errorKind": classify_dm_error(last_error),
            "accountCount": len(accounts),
            "noAccounts": not accounts,
            "accounts": items,
            "nextRunAt": self.next_run_text(),
        }

    def run_sync(self, trigger: str = "manual", wait_seconds: float = 0) -> dict:
        acquired = self._sync_lock.acquire(blocking=False)
        if not acquired and wait_seconds > 0:
            # 手动"立即同步"遇到后台持续轮询时，等一轮结束再执行，而不是直接报忙。
            acquired = self._sync_lock.acquire(timeout=float(wait_seconds))
        if not acquired:
            return {
                "ok": False,
                "busy": True,
                "error": f"{self.platform}同步任务正在运行，请稍后重试",
            }

        started_mono = time.monotonic()
        started_at = now_text()
        selected_accounts: list[dict] = []
        account_results: list[dict] = []
        no_accounts = False
        try:
            settings = self.load_settings()
            accounts = self.list_accounts()
            selected_accounts = self.resolve_accounts(accounts, settings)
            if not selected_accounts:
                no_accounts = True
                raise SyncError(self.no_accounts_message())
            self.save_settings(
                last_started_at=started_at,
                last_error="",
                last_run_trigger=trigger,
                last_skip_reasons="{}",
                last_account_name="、".join(
                    self.account_name(item) for item in selected_accounts
                ),
                last_cookie_file="、".join(
                    name for name in (
                        self._cookie_display(item) for item in selected_accounts
                    ) if name
                ),
            )
            account_results = self._scan_accounts(
                settings, selected_accounts, quick=trigger in self.quick_triggers
            )

            succeeded = [item for item in account_results if item.get("ok")]
            failed = [item for item in account_results if not item.get("ok")]
            scanned = sum(int(item.get("scanned") or 0) for item in succeeded)
            imported = sum(int(item.get("imported") or 0) for item in succeeded)
            duplicates = sum(int(item.get("duplicates") or 0) for item in succeeded)
            skipped = sum(int(item.get("skipped") or 0) for item in succeeded)
            ingest_failed = sum(int(item.get("failed") or 0) for item in succeeded)
            ready = sum(int(item.get("auto_replies_ready") or 0) for item in succeeded)
            skip_reasons: dict[str, int] = {}
            items: list[dict] = []
            for result in succeeded:
                for reason, count in (result.get("skip_reasons") or {}).items():
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + int(count or 0)
                items.extend(result.get("items") or [])
            error_summary = "；".join(
                f"{self.account_name(item.get('account'))}："
                f"{item.get('error') or '同步失败'}"
                for item in failed
            )[:1000]
            finished_at = now_text()
            duration_ms = int((time.monotonic() - started_mono) * 1000)
            self.save_settings(
                last_finished_at=finished_at,
                last_success_at=finished_at if succeeded else "",
                last_scanned=scanned,
                last_imported=imported,
                last_duplicates=duplicates,
                last_skipped=skipped,
                last_failed=ingest_failed + len(failed),
                last_duration_ms=duration_ms,
                last_error=error_summary,
                last_skip_reasons=json.dumps(skip_reasons, ensure_ascii=False),
            )
            # 所有账号都被分级调度跳过（还没轮到执行）时，
            # account_results 是空的——这不是失败，只是本轮没轮上。
            # 之前 ok = bool(succeeded) 会把它判成失败，
            # 后端又因为 error 是空串而回一句"同步失败"，
            # 用户看到的就是"抖音：同步失败"，实际上什么都没坏。
            skipped_all = not account_results
            if skipped_all:
                error_summary = ""
            self.log.info(
                "trigger={} quick={} accounts={}/{} scanned={} imported={} duplicates={} "
                "skipped={} ready={} duration={}ms",
                trigger, trigger in self.quick_triggers, len(succeeded),
                len(account_results), scanned, imported, duplicates, skipped, ready,
                duration_ms,
            )
            return {
                "ok": bool(succeeded) or skipped_all,
                "partial": bool(failed and succeeded),
                "skipped_all": skipped_all,
                "skipped_cold": int(self._skipped_cold),
                "trigger": trigger,
                "quick": trigger in self.quick_triggers,
                "parallel_workers": min(self.max_workers, max(1, len(selected_accounts))),
                "account": public_account(selected_accounts[0]),
                "accounts": account_results,
                "timings": [item.get("timings") or {} for item in account_results],
                "scanned": scanned,
                "imported": imported,
                "duplicates": duplicates,
                "skipped": skipped,
                "failed": ingest_failed + len(failed),
                "auto_replies_ready": ready,
                "skip_reasons": skip_reasons,
                "duration_ms": duration_ms,
                "error": error_summary,
                "items": items,
            }
        except Exception as exc:
            message = (
                str(exc) if isinstance(exc, self.error_types)
                else self.friendly_error(exc)
            )
            try:
                self.save_settings(
                    last_finished_at=now_text(),
                    last_error=message,
                    last_run_trigger=trigger,
                    last_duration_ms=int((time.monotonic() - started_mono) * 1000),
                    last_account_name="、".join(
                        self.account_name(item) for item in selected_accounts
                    ),
                )
            except Exception as save_exc:
                self.log.exception("failed to persist sync error: {}", save_exc)
            if no_accounts and self.no_accounts_is_normal():
                # 没登录账号是"还没配置"，不是故障。
                # 用 INFO 而不是 ERROR，避免日志被刷屏、告警被误触发。
                self.log.info(
                    "trigger={} 跳过：{}", trigger, message,
                )
            else:
                self.log.error("trigger={} failed: {}", trigger, message)
            return {"ok": False, "error": message, "no_accounts": no_accounts}
        finally:
            self._sync_lock.release()
            try:
                current = self.load_settings()
                interval = int(current.get("interval_seconds") or 0)
                enabled = bool(current.get("enabled"))
            except Exception:
                interval, enabled = 0, False
            gap = interval if interval > 0 else self.continuous_gap
            if no_accounts and self.no_accounts_is_normal():
                # 没账号时不用 30 秒就重试一次，退到 30 分钟。
                gap = max(gap, self.no_accounts_backoff)
            with self._state_lock:
                self._next_run_at = time.time() + gap if enabled else None

    def _background_loop(self) -> None:
        while True:
            # 心跳只表示"这个循环还活着"：关闭自动同步、没有账号、平台没新消息
            # 都算正常活着；只有循环卡住或线程死了才该被看门狗抓出来。
            runtime_guard.beat(self.guard_name)
            try:
                settings = self.load_settings()
                if not settings.get("enabled"):
                    with self._state_lock:
                        self._next_run_at = None
                    self._wake_event.wait(10)
                    self._wake_event.clear()
                    continue
                now = time.time()
                with self._state_lock:
                    due = self._next_run_at if self._next_run_at is not None else now
                    self._next_run_at = due
                if now < due:
                    self._wake_event.wait(min(5, max(0.5, due - now)))
                    self._wake_event.clear()
                    continue
                result = self.run_sync(self.auto_trigger)
                try:
                    interval = int(self.load_settings().get("interval_seconds") or 0)
                except Exception:
                    interval = 0
                gap = interval if interval > 0 else self.continuous_gap
                if dm_cursor.is_night():
                    # 夜间降频：整体节奏放慢，减少平台侧风控压力。
                    gap = max(gap, dm_cursor.effective_interval(self.failure_backoff))
                if not result.get("ok"):
                    gap = max(gap, self.failure_backoff)
                    if result.get("no_accounts") and self.no_accounts_is_normal():
                        # 没配置账号是正常状态，别每 30 秒重试一次
                        gap = max(gap, self.no_accounts_backoff)
                # 业务结果单独记：同步失败 != 线程故障，连续失败要能在
                # /health/ready 与私信中心的同步状态里看到。
                runtime_guard.beat(
                    self.guard_name,
                    ok=bool(result.get("ok")),
                    error=str(result.get("error") or ""),
                )
                with self._state_lock:
                    self._next_run_at = time.time() + gap
            except Exception as exc:
                runtime_guard.beat(self.guard_name, ok=False, error=str(exc))
                self.log.exception("background loop error: {}", exc)
                with self._state_lock:
                    self._next_run_at = time.time() + self.failure_backoff
                self._wake_event.wait(30)
                self._wake_event.clear()

    @property
    def guard_name(self) -> str:
        """看门狗里用的任务名（与线程名保持一致，便于对照日志）。"""
        return f"{self.key}-dm-sync"

    def start_background(self) -> None:
        with self._state_lock:
            if self._worker_started:
                return
            self._worker_started = True
            self._next_run_at = time.time() + self.initial_delay
        thread = threading.Thread(
            target=self._background_loop,
            name=f"{self.key}-dm-sync",
            daemon=True,
        )
        thread.start()

    def restart_background(self) -> None:
        """线程死掉后重新拉起同步循环（看门狗用）。

        ``start_background`` 是幂等的，线程没了它也不会重建；重启必须先把
        ``_worker_started`` 打回 False。游标和已入库的消息都在库里，重启不会
        重新拉一遍历史——去重靠 ``external_message_id``。
        """
        with self._state_lock:
            self._worker_started = False
        self.log.warning("重新拉起 {} 私信同步线程", self.key)
        self.start_background()
