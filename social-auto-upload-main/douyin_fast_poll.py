# -*- coding: utf-8 -*-
"""给 douyin_dm_sync 追加"快轮询"：每 N 秒只查一个账号的新消息。

设计要点
--------
* **只走 IM 接口**：`api_only=True` 跳过 DOM 会话列表扫描（省掉切换页签 + 解析
  几十行 DOM 的开销），一次会话列表 + 少量消息接口就够。
* **热页面常驻**：`keep_session=True`。之前每轮都关浏览器，每轮都要付 50 秒
  冷启动，30 秒级轮询根本追不上；现在页面留在内存里复用。
* **多账号轮转**：每次 tick 只处理一个账号（round-robin），3 个号 = 每 90 秒
  各看一次；单号 = 每 30 秒看一次。
* **失败即丢会话**：页面卡死/登录失效时丢掉上下文，下一轮重建，不会一直卡着。

环境变量
--------
``FLOWOPS_DM_FAST_POLL``      默认 1，设 0 关闭快轮询
``FLOWOPS_DM_FAST_INTERVAL``  默认 30（秒）
``FLOWOPS_DM_FAST_LIMIT``     每次最多看多少个会话，默认 20
``FLOWOPS_DM_FAST_DAYS``      快轮询只补最近几天，默认 1
"""
from __future__ import annotations

import os
import threading
import time

from loguru import logger

_FAST_LOCK = threading.Lock()
_FAST_STATE: dict = {
    "started": False,
    "stop": threading.Event(),
    "index": 0,
    "rounds": 0,
    "errors": 0,
    "last": {},
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return int(default)


def _fast_accounts() -> list[dict]:
    """当前可轮询的抖音账号（排除被策略暂停的）。"""
    try:
        import account_safety
        from dm_platform_common import account_rows

        rows = account_rows(3, include_paused=False)
        return account_safety.filter_active_accounts(rows)
    except Exception as exc:
        logger.debug("快轮询读取账号失败: {}", exc)
        return []


def _record_fast_state(account: dict, result: dict, error: str) -> None:
    """把快轮询的检查时间/结果写进账号同步健康度（不覆盖历史补采状态）。"""
    try:
        import dm_ops

        account_id = int(account.get("id") or 0)
        if not account_id:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        imported = int((result or {}).get("imported") or 0)
        with dm_ops.get_conn() as conn:
            row = conn.execute(
                "SELECT last_error_kind FROM dm_account_sync_state "
                "WHERE platform='抖音' AND account_key=?",
                (str(account_id),),
            ).fetchone()
            kind = str((row["last_error_kind"] if row else "") or "")
            if error:
                kind = "error"
            elif kind in ("probe_unknown", "error"):
                kind = ""
            conn.execute(
                """INSERT INTO dm_account_sync_state
                   (platform, account_key, account_name, last_success_at,
                    last_error, last_error_kind, last_checked_at, last_imported)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform, account_key) DO UPDATE SET
                     account_name=excluded.account_name,
                     last_checked_at=excluded.last_checked_at,
                     last_error=excluded.last_error,
                     last_error_kind=excluded.last_error_kind,
                     last_success_at=CASE WHEN excluded.last_error=''
                                          THEN excluded.last_checked_at
                                          ELSE dm_account_sync_state.last_success_at END,
                     last_imported=excluded.last_imported""",
                (
                    "抖音", str(account_id), str(account.get("userName") or ""),
                    now if not error else "", str(error or "")[:500], kind, now, imported,
                ),
            )
    except Exception as exc:
        logger.debug("写入快轮询状态失败: {}", exc)


def fast_poll_account(account: dict) -> dict:
    """对单个账号做一次"只收新消息"的快轮询。"""
    from douyin_dm_sync import _sync_account

    settings = {
        "max_conversations": _env_int("FLOWOPS_DM_FAST_LIMIT", 20),
        "history_days": _env_int("FLOWOPS_DM_FAST_DAYS", 1),
        "recent_only": True,
    }
    started = time.monotonic()
    result = _sync_account(
        settings, account, quick=True, api_only=True, keep_session=True,
    )
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    result["fastPoll"] = True
    return result


def start_fast_poll(interval: int | None = None) -> bool:
    if _env_int("FLOWOPS_DM_FAST_POLL", 1) <= 0:
        logger.info("FLOWOPS_DM_FAST_POLL=0，私信快轮询未启动")
        return False
    with _FAST_LOCK:
        if _FAST_STATE["started"]:
            return True
        _FAST_STATE["started"] = True
        _FAST_STATE["stop"].clear()
    gap = max(10, int(interval or _env_int("FLOWOPS_DM_FAST_INTERVAL", 30)))

    def _loop() -> None:
        runtime_guard = None
        try:
            import runtime_guard as _guard

            runtime_guard = _guard
            runtime_guard.register(
                "dm-fast-poll", description="私信快轮询（只收新消息）",
                interval=max(60, gap * 2), launcher=restart_fast_poll,
            )
        except Exception:
            pass
        while not _FAST_STATE["stop"].wait(gap):
            accounts = _fast_accounts()
            if not accounts:
                continue
            index = _FAST_STATE["index"] % len(accounts)
            _FAST_STATE["index"] = index + 1
            account = accounts[index]
            try:
                result = fast_poll_account(account)
                _FAST_STATE["rounds"] += 1
                _FAST_STATE["last"] = {
                    "at": time.strftime("%H:%M:%S"),
                    "account": account.get("userName") or "",
                    "durationMs": result.get("duration_ms"),
                    "imported": int(result.get("imported") or 0),
                    "scanned": int(result.get("scanned") or 0),
                }
                _record_fast_state(account, result, "")
                logger.info(
                    "快轮询完成 账号={} 耗时={}ms 扫描={} 新入库={}",
                    account.get("userName"), result.get("duration_ms"),
                    result.get("scanned"), result.get("imported"),
                )
                if runtime_guard is not None:
                    runtime_guard.beat("dm-fast-poll")
            except Exception as exc:
                _FAST_STATE["errors"] += 1
                logger.warning("快轮询失败 账号={}: {}", account.get("userName"), exc)
                _record_fast_state(account, {}, str(exc)[:200])
                try:
                    from douyin_dm_sync import drop_sessions_for

                    # 页面可能已经僵死：丢掉该账号的常驻会话，下一轮重建
                    drop_sessions_for(int(account.get("id") or 0))
                except Exception:
                    pass
                if runtime_guard is not None:
                    runtime_guard.beat("dm-fast-poll", ok=False, error=str(exc))

    threading.Thread(target=_loop, name="dm-fast-poll", daemon=True).start()
    logger.info("私信快轮询已启动：每 {} 秒轮一个账号（只收新消息）", gap)
    return True


def restart_fast_poll() -> None:
    """看门狗用：线程意外退出后重新拉起。"""
    with _FAST_LOCK:
        _FAST_STATE["started"] = False
    start_fast_poll()


def stop_fast_poll() -> None:
    with _FAST_LOCK:
        _FAST_STATE["started"] = False
    _FAST_STATE["stop"].set()


def fast_poll_status() -> dict:
    return {
        "started": bool(_FAST_STATE["started"]),
        "interval": _env_int("FLOWOPS_DM_FAST_INTERVAL", 30),
        "limit": _env_int("FLOWOPS_DM_FAST_LIMIT", 20),
        "rounds": int(_FAST_STATE["rounds"]),
        "errors": int(_FAST_STATE["errors"]),
        "last": dict(_FAST_STATE["last"]),
    }
