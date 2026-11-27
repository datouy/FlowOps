# -*- coding: utf-8 -*-
"""会话保活引擎：把"账号别掉线"从副作用变成一条受监管的固定流程。

为什么不是"加个定时任务"
------------------------
1. **一账号一写者**：探针必须和发布、私信拿着同一把 ``account_serial`` 租约，
   否则平台看到的是"同一账号多设备并发"，越保活越容易被挤下线。
2. **只做首页级触达**：2026-11-27 实验证明深访问（数据中心/消息页）不会续期，
   只会多打一次 ``/passport/web/challenge/`` 风控挑战。
3. **失败要分类**：网络抖动、风控验证、真失效是三种完全不同的事；
   只有 ``expired`` 才允许推动账号健康状态，否则就会出现"假死变真死"。
4. **要有账本**：每次触达写 ``account_session_ledger``，窗口剩余天数可查可预警。

调度口径（默认值都可用环境变量覆盖）
------------------------------------
======  ==================  ============================
平台     基线间隔             依据
======  ==================  ============================
抖音     36h ±30%            设备票（bd_ticket_guard_*）1~5 天
小红书   24h ±30%
视频号   30h ±30%（串行）    多开会互相挤下线
其他     48h ±30%
======  ==================  ============================

只在 ``FLOWOPS_KEEPER_ACTIVE_HOURS``（默认 8-23）内触达，避免 24h 均匀心跳的机器特征。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

import session_ledger
import session_probe
from conf import BASE_DIR
from db_utils import connect
from utils.account_serial import account_key, account_serial

# 平台 → 基线小时数
BASE_HOURS = {1: 24.0, 2: 30.0, 3: 36.0, 4: 48.0, 5: 48.0, 6: 48.0}

#: 串行平台：同一时刻只允许一个探针（视频号多开会互相踢）
SERIAL_PLATFORMS = {2}

_STATE_LOCK = threading.Lock()
_SUPERVISOR: threading.Thread | None = None
_SUPERVISOR_STOP = threading.Event()
_LAST_ROUND: dict[str, Any] = {"at": "", "results": [], "error": ""}
_LAST_ROUND_LOCK = threading.Lock()


# --------------------------------------------------------------- 配置

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return int(default)


def enabled() -> bool:
    """总开关；``FLOWOPS_KEEPER=0`` 可整体停用（不影响巡检）。"""
    raw = str(os.environ.get("FLOWOPS_KEEPER", "1")).strip().lower()
    return raw not in ("0", "off", "false", "no")


def base_hours(platform: int | str) -> float:
    """单个平台的基线间隔，支持 ``FLOWOPS_KEEPER_HOURS_DOUYIN=12`` 这类覆盖。"""
    platform_id = int(platform or 0)
    names = {1: "XHS", 2: "CHANNELS", 3: "DOUYIN", 4: "KS", 5: "WEIBO", 6: "BAIJIAHAO"}
    override = os.environ.get(f"FLOWOPS_KEEPER_HOURS_{names.get(platform_id, 'OTHER')}", "")
    if override:
        try:
            return max(1.0, float(override))
        except ValueError:
            pass
    return max(1.0, _env_float("FLOWOPS_KEEPER_BASE_HOURS", BASE_HOURS.get(platform_id, 48.0)))


def jitter_ratio() -> float:
    return max(0.0, min(_env_float("FLOWOPS_KEEPER_JITTER", 0.3), 0.9))


def active_window() -> tuple[int, int]:
    raw = str(os.environ.get("FLOWOPS_KEEPER_ACTIVE_HOURS", "8-23")).strip()
    try:
        start_text, end_text = raw.split("-", 1)
        start, end = int(start_text), int(end_text)
    except ValueError:
        return 8, 23
    start = max(0, min(start, 23))
    end = max(0, min(end, 23))
    return (start, end) if start <= end else (end, start)


def in_active_window(moment: datetime | None = None) -> bool:
    start, end = active_window()
    return start <= (moment or datetime.now()).hour <= end


def warn_days() -> float:
    return max(1.0, _env_float("FLOWOPS_KEEPER_WARN_DAYS", 30.0))


def poll_seconds() -> int:
    return max(30, _env_int("FLOWOPS_KEEPER_POLL_SECONDS", 300))


def max_concurrency() -> int:
    return max(1, min(_env_int("FLOWOPS_KEEPER_MAX_CONCURRENCY", 2), 4))


def probe_headless() -> bool:
    raw = str(os.environ.get("FLOWOPS_KEEPER_HEADLESS", "1")).strip().lower()
    return raw not in ("0", "false", "no", "off")


# --------------------------------------------------------------- 调度

def _stable_jitter(platform: int | str, account_id: int, salt: str = "") -> float:
    """按账号稳定散列产生 ±jitter 的偏移，避免所有账号整点同时触达。"""
    raw = f"{platform}:{account_id}:{salt}".encode("utf-8", errors="ignore")
    digest = hashlib.sha256(raw).digest()
    unit = int.from_bytes(digest[:6], "big") / float(1 << 48)  # 0..1
    return (unit * 2.0 - 1.0) * jitter_ratio()


def next_due_at(
    platform: int | str,
    account_id: int,
    last_touch: datetime | None,
    *,
    now: datetime | None = None,
) -> datetime:
    """下一次应该触达的时间；从未触达过则立刻到期。"""
    moment = now or datetime.now()
    hours = base_hours(platform)
    hours *= 1.0 + _stable_jitter(platform, account_id)
    if last_touch is None:
        return moment
    return last_touch + timedelta(hours=max(1.0, hours))


def is_due(account: dict, *, now: datetime | None = None) -> bool:
    moment = now or datetime.now()
    last = session_ledger.last_touched_at(int(account["id"]))
    return moment >= next_due_at(account.get("type"), int(account["id"]), last, now=moment)


def _accounts() -> list[dict]:
    """活跃账号（排除被策略暂停的），账号库仍是唯一事实来源。"""
    try:
        import account_safety

        account_safety.ensure_schema(BASE_DIR)
        with connect(Path(BASE_DIR) / "db" / "database.db", timeout=30) as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM user_info ORDER BY id")]
        return account_safety.filter_active_accounts(rows)
    except Exception as exc:
        logger.warning("读取账号列表失败，跳过本轮保活: {}", exc)
        return []


def _cookie_path(account: dict) -> Path:
    return Path(BASE_DIR) / "cookiesFile" / str(account.get("filePath") or "")


# --------------------------------------------------------------- 循环线程

class _KeeperLoop:
    """一个专用事件循环：共享 Chromium 分片必须跨轮次复用同一 loop。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run, name="session-keeper-loop", daemon=True,
            )
            self._thread.start()
        self._ready.wait(10)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def run(self, coro, timeout: float = 900.0):
        self.start()
        loop = self._loop
        if loop is None:
            raise RuntimeError("保活事件循环未就绪")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout)

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


_LOOP = _KeeperLoop()


# --------------------------------------------------------------- 触达实现

class _Lease:
    """把同步的 ``account_serial`` 包成可在线程里获取/释放的句柄。"""

    def __init__(self, key: str, timeout: float = 600.0) -> None:
        self.key = key
        self.timeout = timeout
        self._cm = None

    def acquire(self) -> None:
        self._cm = account_serial(self.key, timeout=self.timeout)
        self._cm.__enter__()

    def release(self) -> None:
        handle, self._cm = self._cm, None
        if handle is None:
            return
        try:
            handle.__exit__(None, None, None)
        except Exception:
            pass


async def _touch_douyin(cookie_path: Path, account: dict) -> dict[str, Any]:
    from patchright.async_api import async_playwright
    from utils.account_browser import (
        persistent_account_context,
        save_account_storage_state,
    )

    evidence: list[dict[str, Any]] = []
    stop_seconds = _env_float("FLOWOPS_KEEPER_STOP_SECONDS", 6.0)
    main_site = str(os.environ.get("FLOWOPS_KEEPER_MAIN_SITE", "1")).lower() not in (
        "0", "off", "false", "no",
    )
    async with async_playwright() as playwright:
        async with persistent_account_context(
            playwright,
            cookie_path,
            account_id=account.get("id"),
            headless=probe_headless(),
            channel="chromium",
        ) as context:
            result = await session_probe.touch_douyin(
                context, stop_seconds=stop_seconds, main_site=main_site, evidence=evidence,
            )
            # 触达后立刻把刷新到的 cookie 写回加密快照（共享分片/迁移都吃这份）
            try:
                await save_account_storage_state(context, cookie_path, merge_existing=True)
            except Exception as exc:
                logger.debug("保活触达后回写登录态失败（不影响本次判定）: {}", exc)
    result["evidence"] = evidence
    return result


async def _touch_other(account: dict) -> dict[str, Any]:
    """非抖音平台：复用既有存活校验（它本身会开持久 profile 并回写登录态）。"""
    from myUtils.auth import check_cookie

    alive = bool(await check_cookie(account.get("type"), account.get("filePath")))
    return {
        "outcome": "checked_only" if alive else "expired",
        "navigation": [],
        "sessionChanges": [],
        "deviceChanges": [],
        "window": {"issuedAt": "", "expiresAt": "", "validSeconds": 0, "validDays": 0.0, "source": ""},
        "cookieTotal": 0,
        "cookieChanged": 0,
        "alive": alive,
        "error": "" if alive else "存活校验未通过",
    }


def _has_warm_session(account_id: int) -> bool:
    """抖音私信是否已有常驻标签页在维持活跃（P2 warm channel）。

    抖音私信同步会长时间保留"一账号一 context"，每轮 reload 一次聊天页。
    那就是最接近官方网页端的在线状态：没必要再开第二个浏览器抢同一份 profile。
    """
    try:
        import douyin_dm_sync

        return int(account_id) in douyin_dm_sync.live_session_ids()
    except Exception:
        return False


async def touch_account(account: dict, *, probe: Any = None) -> dict[str, Any]:
    """对一个账号做一次触达并落账本。失败不外抛，返回结构化结果。"""
    account_id = int(account["id"])
    platform = int(account.get("type") or 0)
    cookie_path = _cookie_path(account)
    started = time.monotonic()

    # 已有常驻通道在维持活跃：本轮不重复开浏览器（避免同账号双开被挤下线）
    if platform == 3 and probe is None and _has_warm_session(account_id):
        payload = _finish(
            account, "deferred", started, source="dm-warm",
            error="抖音私信常驻标签页在运行，本轮不额外触达",
        )
        _maybe_warn(account, payload)
        return payload

    lease = _Lease(
        account_key(platform, account.get("userName"), account_id, account.get("filePath")),
        timeout=_env_float("FLOWOPS_KEEPER_LEASE_TIMEOUT", 240.0),
    )
    try:
        await asyncio.to_thread(lease.acquire)
    except Exception as exc:
        return _finish(
            account, "network", started,
            error=f"账号正被其他任务占用: {exc}"[:300], source="keeper",
        )

    result: dict[str, Any]
    try:
        runner = probe or (_touch_douyin if platform == 3 else _touch_other)
        if runner is _touch_douyin:
            result = await runner(cookie_path, account)
        else:
            result = await runner(account)
    except Exception as exc:
        logger.warning("账号 {} 保活触达异常: {}", account_id, exc)
        result = {
            "outcome": "probe_failed", "navigation": [], "sessionChanges": [],
            "deviceChanges": [], "window": {}, "cookieTotal": 0, "cookieChanged": 0,
            "alive": True, "error": f"{type(exc).__name__}: {exc}"[:300],
        }
    finally:
        await asyncio.to_thread(lease.release)

    payload = _finish(
        account, str(result.get("outcome") or "unchanged"), started,
        window=result.get("window") or {},
        session_changes=result.get("sessionChanges") or [],
        device_changes=result.get("deviceChanges") or [],
        cookie_total=int(result.get("cookieTotal") or 0),
        cookie_changed=int(result.get("cookieChanged") or 0),
        error=str(result.get("error") or ""),
        navigation=result.get("navigation") or [],
        evidence=result.get("evidence") or [],
    )
    _apply_health(account, payload)
    _maybe_warn(account, payload)
    return payload


def _finish(
    account: dict,
    outcome: str,
    started: float,
    *,
    window: dict[str, Any] | None = None,
    session_changes: list | None = None,
    device_changes: list | None = None,
    cookie_total: int = 0,
    cookie_changed: int = 0,
    error: str = "",
    source: str = "keeper",
    navigation: list | None = None,
    evidence: list | None = None,
) -> dict[str, Any]:
    window = window or {}
    duration_ms = int((time.monotonic() - started) * 1000)
    account_id = int(account["id"])
    platform = int(account.get("type") or 0)
    session_ledger.record(
        account_id,
        platform,
        outcome,
        source=source,
        duration_ms=duration_ms,
        session_issued_at=str(window.get("issuedAt") or ""),
        session_expires_at=str(window.get("expiresAt") or ""),
        session_rotated=bool(session_changes),
        cookie_total=cookie_total,
        cookie_changed=cookie_changed,
        error=error,
    )
    return {
        "accountId": account_id,
        "accountName": account.get("userName") or "",
        "platform": platform,
        "outcome": outcome,
        "durationMs": duration_ms,
        "sessionChanges": session_changes or [],
        "deviceChanges": device_changes or [],
        "window": window,
        "licenseLeft": session_ledger.window_days_left(account_id),
        "cookieTotal": cookie_total,
        "cookieChanged": cookie_changed,
        "navigation": navigation or [],
        "evidence": evidence or [],
        "error": error,
    }


def _apply_health(account: dict, payload: dict[str, Any]) -> None:
    """只有 ``expired`` 才推健康状态；其余只留证据（逻辑在 account_store）。"""
    try:
        from account_store import get_store

        store = get_store(BASE_DIR)
        store.mark_probe(
            int(account["id"]),
            str(payload.get("outcome") or ""),
            str(payload.get("error") or ""),
        )
    except Exception as exc:
        logger.debug("保活回写账号健康度失败: {}", exc)


def _maybe_warn(account: dict, payload: dict[str, Any]) -> None:
    """窗口剩余不足阈值时提前预警：把"突然掉线"变成"计划内重扫"。"""
    days_left = payload.get("licenseLeft")
    if days_left is None or days_left > warn_days():
        return
    account_id = int(account["id"])
    if session_ledger.warned_recently(account_id, days=7):
        return
    session_ledger.record(
        account_id, int(account.get("type") or 0), "window_warning",
        source="keeper",
        error=f"登录窗口剩余 {days_left:.1f} 天，请安排重新扫码",
    )
    try:
        from notify_ops import notify

        notify(
            "账号登录窗口即将到期",
            f"平台：{session_probe_label(account.get('type'))}\n"
            f"账号：{account.get('userName')}\n"
            f"剩余：{days_left:.1f} 天\n"
            f"请在此之前重新扫码，避免发布/私信中断。",
            "warning",
        )
    except Exception as exc:
        logger.debug("发送窗口预警失败（不影响保活）: {}", exc)


def session_probe_label(platform: Any) -> str:
    return {
        1: "小红书", 2: "视频号", 3: "抖音", 4: "快手", 5: "微博", 6: "百家号",
    }.get(int(platform or 0), f"平台{platform}")


# --------------------------------------------------------------- 对外入口

def run_keeper_once(
    *,
    account_ids: list[int] | None = None,
    force: bool = False,
    dry_run: bool = False,
    probe: Any = None,
) -> list[dict[str, Any]]:
    """跑一轮保活（同步入口，供 HTTP 接口与单测调用）。"""
    if not enabled():
        return []
    accounts = _accounts()
    if account_ids:
        wanted = {int(value) for value in account_ids}
        accounts = [item for item in accounts if int(item["id"]) in wanted]
    if not force and not in_active_window():
        logger.debug("不在保活活跃时段 {}，本轮跳过", active_window())
        return []
    todo: list[dict] = []
    for account in accounts:
        if force or is_due(account):
            todo.append(account)
    if dry_run:
        return [
            {
                "accountId": int(item["id"]),
                "accountName": item.get("userName") or "",
                "platform": int(item.get("type") or 0),
                "due": is_due(item),
            }
            for item in todo
        ]
    if not todo:
        return []
    results = _LOOP.run(_run_round(todo, probe=probe))
    with _LAST_ROUND_LOCK:
        _LAST_ROUND.update({
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "results": results,
            "error": "",
        })
    return results


async def _run_round(accounts: list[dict], *, probe: Any = None) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(max_concurrency())
    serial_locks = {platform: asyncio.Lock() for platform in SERIAL_PLATFORMS}

    async def _one(account: dict) -> dict[str, Any]:
        platform = int(account.get("type") or 0)
        lock = serial_locks.get(platform)
        async with semaphore:
            if lock is not None:
                async with lock:
                    return await touch_account(account, probe=probe)
            return await touch_account(account, probe=probe)

    tasks = [_one(account) for account in accounts]
    done = await asyncio.gather(*tasks, return_exceptions=True)
    results: list[dict[str, Any]] = []
    for account, item in zip(accounts, done):
        if isinstance(item, Exception):
            logger.warning("账号 {} 保活任务异常: {}", account.get("id"), item)
            results.append({
                "accountId": account.get("id"), "outcome": "probe_failed",
                "error": f"{type(item).__name__}: {item}"[:300],
            })
        else:
            results.append(item)
    return results


def start_keeper() -> bool:
    """启动受监管的保活循环；重复调用安全。"""
    global _SUPERVISOR
    if not enabled():
        logger.info("FLOWOPS_KEEPER=0，保活循环未启动")
        return False
    with _STATE_LOCK:
        if _SUPERVISOR is not None and _SUPERVISOR.is_alive():
            return True
        _SUPERVISOR_STOP.clear()
        # 提前把探针事件循环拉起来：共享 Chromium 分片必须跨轮次复用同一个
        # loop，也让 /api/v2/keeper/status 的 loopAlive 从启动那一刻就可信。
        _LOOP.start()
        _SUPERVISOR = threading.Thread(
            target=_supervise, name="session-keeper", daemon=True,
        )
        _SUPERVISOR.start()
    logger.info(
        "会话保活已启动：基线 抖音 {}h / 小红书 {}h / 视频号 {}h，活跃时段 {}",
        base_hours(3), base_hours(1), base_hours(2), active_window(),
    )
    return True


def _supervise() -> None:
    runtime_guard = None
    try:
        import runtime_guard as _guard

        runtime_guard = _guard
        runtime_guard.register(
            "account-keeper", description="账号会话保活触达",
            interval=2 * poll_seconds(), launcher=restart_keeper,
        )
    except Exception:
        logger.debug("保活循环未能登记到 runtime_guard，将独立运行")
    while not _SUPERVISOR_STOP.wait(poll_seconds()):
        try:
            run_keeper_once()
            if runtime_guard is not None:
                runtime_guard.beat("account-keeper")
        except Exception as exc:
            logger.exception("保活循环异常")
            if runtime_guard is not None:
                runtime_guard.beat("account-keeper", ok=False, error=str(exc))


def restart_keeper() -> None:
    """看门狗用：线程意外退出后重新拉起。"""
    global _SUPERVISOR
    with _STATE_LOCK:
        _SUPERVISOR = None
    start_keeper()


def stop_keeper(timeout: float = 5.0) -> None:
    global _SUPERVISOR
    _SUPERVISOR_STOP.set()
    thread, _SUPERVISOR = _SUPERVISOR, None
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


def keeper_status(limit: int = 50) -> dict[str, Any]:
    """前端/运维看板：每个账号的剩余天数、上次结论、下次触达时间。"""
    accounts = _accounts()
    items = []
    now = datetime.now()
    for account in accounts[: max(1, int(limit or 50))]:
        account_id = int(account["id"])
        last = session_ledger.last_touched_at(account_id)
        state = session_ledger.state_for(account_id)
        items.append({
            "accountId": account_id,
            "accountName": account.get("userName") or "",
            "platform": int(account.get("type") or 0),
            "platformLabel": session_probe_label(account.get("type")),
            "status": int(account.get("status") or 0),
            "lastTouchedAt": state.get("touchedAt") or "",
            "lastOutcome": state.get("outcome") or "",
            "daysLeft": state.get("daysLeft"),
            "sessionExpiresAt": state.get("sessionExpiresAt") or "",
            "renewed30d": state.get("renewed30d") or 0,
            "deviceRefreshed30d": state.get("deviceRefreshed30d") or 0,
            "nextDueAt": next_due_at(account.get("type"), account_id, last, now=now)
            .strftime("%Y-%m-%d %H:%M:%S"),
            "due": is_due(account, now=now),
            "warningAt": state.get("warningAt") or "",
        })
    with _LAST_ROUND_LOCK:
        last_round = dict(_LAST_ROUND)
    return {
        "enabled": enabled(),
        "running": bool(_SUPERVISOR and _SUPERVISOR.is_alive()),
        "loopAlive": _LOOP.alive(),
        "activeWindow": active_window(),
        "inActiveWindow": in_active_window(),
        "maxConcurrency": max_concurrency(),
        "warnDays": warn_days(),
        "pollSeconds": poll_seconds(),
        "baseHours": {session_probe_label(key): base_hours(key) for key in BASE_HOURS},
        "lastRound": last_round,
        "accounts": items,
    }


def reset_for_tests() -> None:
    global _SUPERVISOR
    with _STATE_LOCK:
        _SUPERVISOR = None
    _SUPERVISOR_STOP.set()
    with _LAST_ROUND_LOCK:
        _LAST_ROUND.update({"at": "", "results": [], "error": ""})
