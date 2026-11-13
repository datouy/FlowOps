# -*- coding: utf-8 -*-
"""后台任务的守护、心跳与健康聚合。

**现在的问题**：项目里有十几处裸 ``daemon`` 线程——发布 worker 池、调度
循环、追踪循环、账号巡检、stats 回流、评论同步、三平台私信同步。每个循环
自己 ``try/except`` 兜住**循环体**，但一旦**线程本身**没了（未被捕获的
异常逃出了 try、死锁、Event 被永久清除、loop 被关掉），没有任何人知道：

* 界面照旧显示"自动同步运行中"，其实已经三四天没同步过一条私信；
* 定时发布不再触发，用户以为"平台没发出去"；
* 发布 worker 全死光，任务永远停在 ``pending``。

这类故障不会报错、不会告警，只会"慢慢不干活"——是所有后台型产品最贵的
一类 bug。

**设计**：

1. ``register(name, launcher=...)`` 登记任务：名字、循环周期、可选的
   **重启器**。重启器必须幂等（各模块的 ``start_*`` 基本都是）。
2. ``beat(name)`` 由任务循环每跑一圈调用一次，只写一个时间戳——
   **不改控制流**，也**不吞异常**。心跳语义是"这个循环还活着"，
   不是"业务成功"；业务结果用 ``beat(name, ok=False, error=...)``。
3. watchdog 线程每 ``WATCHDOG_INTERVAL`` 秒巡检一次：
   * 线程存在且已死 → 调 launcher 重启（指数退避，最多 ``max_restarts`` 次）；
   * 心跳超过 ``interval * stale_factor`` 未更新 → 判为"卡死"，记事故，
     能重启的重启（卡死通常是阻塞在某个网络等待上）。
4. ``status()`` 输出给 ``/health/ready`` 与前端「运行状态」页。

**取舍**：不引入线程池、不引入调度框架、不落库。私有化环境要能零依赖跑，
所以状态全在内存，进程重启即重置——这没问题，因为"进程重启"本身就是
一次全量重置。

**配置**（环境变量）：

* ``FLOWOPS_RUNTIME_GUARD`` —— 关闭看门狗（``0``/``off``），默认开
* ``FLOWOPS_RUNTIME_GUARD_INTERVAL`` —— 巡检间隔秒数，默认 30
* ``FLOWOPS_RUNTIME_STALE_FACTOR`` —— 心跳超时倍数，默认 3
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Any, Callable

from loguru import logger


# ---------------------------------------------------------------- 配置

def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def guard_enabled() -> bool:
    raw = str(os.environ.get("FLOWOPS_RUNTIME_GUARD", "1")).strip().lower()
    return raw not in ("0", "off", "false", "no")


def watchdog_interval() -> int:
    return _env_int("FLOWOPS_RUNTIME_GUARD_INTERVAL", 30)


def stale_factor() -> int:
    return _env_int("FLOWOPS_RUNTIME_STALE_FACTOR", 3)


# ---------------------------------------------------------------- 状态

class _Task:
    """一个被守护的后台任务的运行状态。"""

    __slots__ = (
        "name", "description", "interval", "launcher", "max_restarts",
        "registered_at", "started_at", "beat_at", "ok_at", "error",
        "consecutive_failures", "restarts", "restart_at", "stalled",
        "generation", "thread",
    )

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        interval: float = 60.0,
        launcher: Callable[[], Any] | None = None,
        max_restarts: int = 5,
    ) -> None:
        self.name = name
        self.description = description
        self.interval = float(interval or 60.0)
        self.launcher = launcher
        self.max_restarts = int(max_restarts)
        self.registered_at = time.time()
        self.started_at = 0.0
        self.beat_at = 0.0
        self.ok_at = 0.0
        self.error = ""
        self.consecutive_failures = 0
        self.restarts = 0
        self.restart_at = 0.0
        self.stalled = False
        self.generation = 0
        self.thread: threading.Thread | None = None

    def alive(self) -> bool | None:
        """``True`` 活着 / ``False`` 已死 / ``None`` 没绑线程（按心跳判）。"""
        if self.thread is None:
            return None
        return self.thread.is_alive()

    def beat_age(self) -> float | None:
        if not self.beat_at:
            return None
        return round(time.time() - self.beat_at, 1)

    def stale_after(self) -> float:
        return max(self.interval * stale_factor(), self.interval + 60.0)

    def to_dict(self) -> dict[str, Any]:
        age = self.beat_age()
        alive = self.alive()
        return {
            "name": self.name,
            "description": self.description,
            "interval": self.interval,
            "alive": alive,
            "beatAge": age,
            "staleAfter": round(self.stale_after(), 1),
            "stalled": bool(self.stalled),
            "lastOkAge": (
                round(time.time() - self.ok_at, 1) if self.ok_at else None
            ),
            "error": self.error,
            "consecutiveFailures": self.consecutive_failures,
            "restarts": self.restarts,
            "generation": self.generation,
            "restartable": self.launcher is not None,
        }


_tasks: dict[str, _Task] = {}
_guard = threading.RLock()
_watchdog: threading.Thread | None = None
_watchdog_stop = threading.Event()
_watchdog_state = {"ticks": 0, "last_tick_at": 0.0, "actions": 0}


# ---------------------------------------------------------------- 注册与心跳

def register(
    name: str,
    *,
    description: str = "",
    interval: float = 60.0,
    launcher: Callable[[], Any] | None = None,
    max_restarts: int = 5,
) -> _Task:
    """登记一个后台任务。重复登记会更新元信息但保留运行状态。"""
    key = str(name or "").strip()
    if not key:
        raise ValueError("后台任务必须有名字")
    with _guard:
        task = _tasks.get(key)
        if task is None:
            task = _Task(
                key,
                description=description,
                interval=interval,
                launcher=launcher,
                max_restarts=max_restarts,
            )
            _tasks[key] = task
        else:
            task.description = description or task.description
            task.interval = float(interval or task.interval)
            if launcher is not None:
                task.launcher = launcher
            task.max_restarts = int(max_restarts)
    return task


def bind_thread(name: str, thread: threading.Thread | None = None) -> None:
    """把任务和它的线程绑起来，供 watchdog 判存活。

    不传 ``thread`` 时，取当前线程。
    """
    target = thread or threading.current_thread()
    with _guard:
        task = _tasks.get(str(name or "").strip())
        if task is None:
            return
        task.thread = target
        task.started_at = time.time()
        if not task.beat_at:
            task.beat_at = time.time()


def beat(name: str, ok: bool = True, error: str = "", detail: str = "") -> None:
    """打一次心跳。任务循环每跑一圈调用。

    ``ok=False`` 表示这一轮业务失败（异常、同步失败、平台拒绝），
    会累计 ``consecutiveFailures`` 并保留最后一次错误，但**不影响**"循环
    还活着"的判断——失败是业务态，卡死才是故障态。
    """
    now = time.time()
    with _guard:
        task = _tasks.get(str(name or "").strip())
        if task is None:
            # 未登记的心跳自动登记（interval 取默认），避免调用点必须先注册。
            task = _Task(str(name or "").strip())
            _tasks[task.name] = task
        task.beat_at = now
        if task.thread is None:
            task.thread = threading.current_thread()
            task.started_at = now
        if ok:
            task.ok_at = now
            task.consecutive_failures = 0
            task.error = ""
            task.stalled = False
        else:
            task.consecutive_failures += 1
            task.error = str(error or detail or "未知错误")[:300]


def fail(name: str, error: str) -> None:
    """等价于 ``beat(name, ok=False, error=...)``。"""
    beat(name, ok=False, error=error)


def unregister(name: str) -> None:
    with _guard:
        _tasks.pop(str(name or "").strip(), None)


def current_generation(name: str) -> int:
    """返回任务当前代际。后台循环每轮比对，不匹配则退出，避免旧代残留。"""
    with _guard:
        task = _tasks.get(str(name or "").strip())
        return task.generation if task is not None else 0


def is_current(name: str, generation: int) -> bool:
    """当前循环是否仍是最新代际。"""
    return generation == current_generation(name)


# ---------------------------------------------------------------- 巡检

def _restart(task: _Task, reason: str) -> None:
    """尝试重启一个任务。失败只记录，不抛。"""
    if task.launcher is None:
        logger.error("后台任务 {} {}，且没有重启器，需要人工介入", task.name, reason)
        return
    if task.restarts >= task.max_restarts:
        logger.error(
            "后台任务 {} {}，已重启 {} 次达到上限，停止自动重启",
            task.name, reason, task.restarts,
        )
        return
    # 指数退避：30s、60s、120s……最多 10 分钟
    backoff = min(30.0 * (2 ** task.restarts), 600.0)
    now = time.time()
    if task.restart_at and now - task.restart_at < backoff:
        return
    task.restart_at = now
    task.restarts += 1
    logger.warning(
        "后台任务 {} {}，第 {} 次重启（退避 {}s 已过）",
        task.name, reason, task.restarts, int(backoff),
    )
    try:
        task.launcher()
    except Exception as exc:
        logger.exception("后台任务 {} 重启失败：{}", task.name, exc)
        task.error = f"重启失败：{exc}"[:300]
        return
    _watchdog_state["actions"] += 1


def check_once() -> list[dict[str, Any]]:
    """跑一轮巡检，返回本轮发生的动作（便于测试断言）。"""
    actions: list[dict[str, Any]] = []
    now = time.time()
    with _guard:
        snapshot = list(_tasks.values())
    for task in snapshot:
        alive = task.alive()
        age = task.beat_age()
        stale = age is not None and age > task.stale_after()
        if alive is False:
            task.stalled = True
            actions.append({"name": task.name, "action": "restart", "reason": "线程已退出"})
            _restart(task, "线程已退出")
            task.generation += 1
            continue
        if stale:
            # 线程还在但心跳停了：通常是阻塞在网络等待上（平台不响应、
            # 代理挂死、CDP 会话僵住）。先记事故，再重启一次。
            if not task.stalled:
                logger.error(
                    "后台任务 {} 心跳已停 {:.0f}s（阈值 {:.0f}s），疑似卡死",
                    task.name, age, task.stale_after(),
                )
            task.stalled = True
            actions.append({"name": task.name, "action": "restart", "reason": f"心跳停滞 {age}s"})
            _restart(task, f"心跳停滞 {age:.0f}s")
            task.generation += 1
    _watchdog_state["ticks"] += 1
    _watchdog_state["last_tick_at"] = now
    return actions


def _watchdog_loop() -> None:
    while not _watchdog_stop.is_set():
        try:
            check_once()
        except Exception:
            logger.exception("后台守护巡检异常")
            traceback.print_exc()
        _watchdog_stop.wait(watchdog_interval())


def start_watchdog() -> bool:
    """启动看门狗线程（幂等）。返回是否由本次调用启动。"""
    global _watchdog
    if not guard_enabled():
        logger.info("后台守护已通过 FLOWOPS_RUNTIME_GUARD 关闭")
        return False
    with _guard:
        if _watchdog is not None and _watchdog.is_alive():
            return False
        _watchdog_stop.clear()
        _watchdog = threading.Thread(
            target=_watchdog_loop, name="runtime-guard", daemon=True,
        )
        _watchdog.start()
    logger.info("后台守护已启动，巡检间隔 {}s", watchdog_interval())
    return True


def stop_watchdog() -> None:
    global _watchdog
    _watchdog_stop.set()
    thread = _watchdog
    _watchdog = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)


# ---------------------------------------------------------------- 对外状态

def status() -> dict[str, Any]:
    """所有被守护任务的健康快照。"""
    with _guard:
        tasks = [task.to_dict() for task in _tasks.values()]
    degraded = [
        item for item in tasks
        if item["alive"] is False or item["stalled"]
    ]
    return {
        "enabled": guard_enabled(),
        "watchdogAlive": bool(_watchdog is not None and _watchdog.is_alive()),
        "ticks": _watchdog_state["ticks"],
        "actions": _watchdog_state["actions"],
        "degraded": [item["name"] for item in degraded],
        "tasks": tasks,
    }


def healthy(ignore: tuple[str, ...] = ()) -> tuple[bool, list[str]]:
    """给就绪探针用的布尔判定：``(是否健康, 不健康的任务名)``。"""
    snapshot = status()
    bad = [name for name in snapshot["degraded"] if name not in ignore]
    return (not bad, bad)


def reset_for_tests() -> None:
    """清空注册表与统计（只给测试用）。"""
    with _guard:
        _tasks.clear()
        _watchdog_state.update({"ticks": 0, "last_tick_at": 0.0, "actions": 0})
