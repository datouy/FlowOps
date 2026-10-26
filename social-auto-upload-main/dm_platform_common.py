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
import dm_ops
from db_utils import apply_migrations, connect

ACCOUNT_DB = Path(BASE_DIR) / "db" / "database.db"
COOKIE_DIR = Path(BASE_DIR) / "cookiesFile"


class SyncError(RuntimeError):
    """可直接展示给用户的同步错误。"""


def friendly_error(exc: Exception) -> str:
    """把底层异常转换成适合展示给用户的中文提示。"""
    text = str(exc).strip() or exc.__class__.__name__
    lower = text.lower()
    if "executable doesn't exist" in lower or "browsertype.launch" in lower:
        return "Playwright 浏览器未安装，请执行 playwright install chromium"
    if "target page, context or browser has been closed" in lower:
        return "平台页面意外关闭，请稍后重试"
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
        auto_trigger: str = "timer",
        initial_delay: float = 5,
        prefer_active_accounts: bool = False,
        error_types: tuple[type[BaseException], ...] = (),
        max_workers: int = 8,
        failure_backoff: float = 30.0,
        continuous_gap: float = 0.5,
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
        self.auto_trigger = auto_trigger
        self.initial_delay = max(0.0, float(initial_delay))
        self.prefer_active_accounts = prefer_active_accounts
        self.error_types = (SyncError, *error_types)
        self.max_workers = max(1, int(max_workers))
        self.failure_backoff = max(1.0, float(failure_backoff))
        self.continuous_gap = max(0.0, float(continuous_gap))
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
            "draft_only": 1,
        }
        self.sync_fields = {
            "enabled", "account_id", "account_ids", "interval_seconds",
            "max_conversations", "recent_only", "draft_only",
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

    def friendly_error(self, exc: Exception) -> str:
        return friendly_error(exc)

    def no_accounts_message(self) -> str:
        return f"未找到{self.platform}账号，请先在账号管理中登录"

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
             recent_only,draft_only)
            VALUES (1,?,?,?,?,?,?,?)""",
            (
                self.default_settings["enabled"],
                self.default_settings["account_id"],
                json.dumps(self.default_settings["account_ids"], ensure_ascii=False),
                self.default_settings["interval_seconds"],
                self.default_settings["max_conversations"],
                self.default_settings["recent_only"],
                self.default_settings["draft_only"],
            ),
        )
        self.init_extra(conn)

    def init_db(self) -> None:
        with dm_ops.get_conn() as conn:
            apply_migrations(
                conn,
                f"dm_sync:{self.settings_table}",
                {1: self._create_schema},
            )

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
        data["recent_window_hours"] = self.recent_hours
        data["parallel_workers"] = min(self.max_workers, max(1, len(selected)))
        data["sync_mode"] = (
            "continuous" if int(settings.get("interval_seconds") or 0) <= 0 else "interval"
        )
        data["read_only"] = True
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
            return jsonify({
                "code": 400, "msg": f"所选{self.platform}账号不存在或已删除",
            }), 400

        enabled = as_flag(data.get("enabled", 1))
        self.save_settings(
            enabled=enabled,
            account_id=account_ids[0] if len(account_ids) == 1 else 0,
            account_ids=account_ids,
            interval_seconds=interval,
            max_conversations=max_conversations,
            recent_only=as_flag(data.get("recent_only", 1)),
            draft_only=1,
        )
        with self._state_lock:
            # 保存后立即触发一轮，持续模式下由 finally 继续排下一轮。
            self._next_run_at = time.time() if enabled else None
        self._wake_event.set()
        return jsonify({
            "code": 200, "data": self.status_payload(), "msg": "同步设置已保存",
        })

    def run_view(self):
        result = self.run_sync("manual")
        if not result.get("ok"):
            status = 409 if result.get("busy") else 400
            return jsonify({
                "code": status,
                "data": result,
                "msg": result.get("error") or "同步失败",
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

    def _scan_accounts(
        self, settings: dict, accounts: list[dict], quick: bool = False,
    ) -> list[dict]:
        """按账号并行扫描；结果顺序与账号顺序一致。"""
        if len(accounts) <= 1 or self.max_workers <= 1:
            return [self._scan_one(settings, account, quick) for account in accounts]
        workers = max(1, min(self.max_workers, len(accounts)))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=f"{self.key}-account",
        ) as pool:
            futures = [
                pool.submit(self._scan_one, settings, account, quick)
                for account in accounts
            ]
            return [future.result() for future in futures]

    def run_sync(self, trigger: str = "manual") -> dict:
        if not self._sync_lock.acquire(blocking=False):
            return {
                "ok": False,
                "busy": True,
                "error": f"{self.platform}同步任务正在运行，请稍后重试",
            }

        started_mono = time.monotonic()
        started_at = now_text()
        selected_accounts: list[dict] = []
        account_results: list[dict] = []
        try:
            settings = self.load_settings()
            accounts = self.list_accounts()
            selected_accounts = self.resolve_accounts(accounts, settings)
            if not selected_accounts:
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
            self.log.info(
                "trigger={} quick={} accounts={}/{} scanned={} imported={} duplicates={} "
                "skipped={} ready={} duration={}ms",
                trigger, trigger in self.quick_triggers, len(succeeded),
                len(account_results), scanned, imported, duplicates, skipped, ready,
                duration_ms,
            )
            return {
                "ok": bool(succeeded),
                "partial": bool(failed and succeeded),
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
            self.log.error("trigger={} failed: {}", trigger, message)
            return {"ok": False, "error": message}
        finally:
            self._sync_lock.release()
            try:
                current = self.load_settings()
                interval = int(current.get("interval_seconds") or 0)
                enabled = bool(current.get("enabled"))
            except Exception:
                interval, enabled = 0, False
            gap = interval if interval > 0 else self.continuous_gap
            with self._state_lock:
                self._next_run_at = time.time() + gap if enabled else None

    def _background_loop(self) -> None:
        while True:
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
                if not result.get("ok"):
                    gap = max(gap, self.failure_backoff)
                with self._state_lock:
                    self._next_run_at = time.time() + gap
            except Exception as exc:
                self.log.exception("background loop error: {}", exc)
                with self._state_lock:
                    self._next_run_at = time.time() + self.failure_backoff
                self._wake_event.wait(30)
                self._wake_event.clear()

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
