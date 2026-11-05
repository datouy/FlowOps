# -*- coding: utf-8 -*-
"""账号级代理出口分配。

严格的“一账号一 profile、一指纹、一代理出口”要求：多个账号不能共用同一个
出口 IP。这里提供：

- 代理池：一份可配置的代理列表，按账号自动分配不重复的出口；
- 共享风险检查：同一个代理被多个账号使用时给出明确告警；
- 绑定状态：账号当前绑定的是哪个代理、是否与其他账号冲突；
- 未配置代理时明确提示“多个账号共用本机 IP”。

代理池的来源是一个 JSON 文件（默认 ``db/proxy_pool.json``），格式::

    {
      "proxies": [
        {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"},
        {"server": "socks5://5.6.7.8:1080"}
      ]
    }

也支持顶层就是一个字符串数组。密码只落在这个文件里，不会出现在接口响应中。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from conf import BASE_DIR

PROXY_POOL_FILE = Path(BASE_DIR) / "db" / "proxy_pool.json"
# 未配置代理时给出的明确提示。
NO_PROXY_WARNING = "未配置代理：多个账号共用本机 IP，出口无法隔离"


def _normalize(entry: Any) -> dict[str, str]:
    if isinstance(entry, str):
        return {"server": entry.strip(), "username": "", "password": ""}
    if not isinstance(entry, dict):
        return {"server": "", "username": "", "password": ""}
    return {
        "server": str(entry.get("server") or "").strip(),
        "username": str(entry.get("username") or "").strip(),
        "password": str(entry.get("password") or ""),
    }


def load_proxy_pool(path: str | Path | None = None) -> list[dict[str, str]]:
    """读取代理池；文件不存在或格式不对时返回空列表。"""
    target = Path(path or os.environ.get("FLOWOPS_PROXY_POOL") or PROXY_POOL_FILE)
    if not target.is_file():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("代理池文件无法解析: {}", target)
        return []
    entries = payload
    if isinstance(payload, dict):
        entries = payload.get("proxies") or []
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries:
        item = _normalize(entry)
        if item["server"]:
            result.append(item)
    return result


def save_proxy_pool(
    proxies: list[Any], path: str | Path | None = None,
) -> list[dict[str, str]]:
    """写入代理池（供接口使用）。"""
    target = Path(path or os.environ.get("FLOWOPS_PROXY_POOL") or PROXY_POOL_FILE)
    normalized = [_normalize(item) for item in proxies or []]
    normalized = [item for item in normalized if item["server"]]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"proxies": normalized}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return normalized


def proxy_key(proxy: dict[str, str] | None) -> str:
    """代理的唯一标识（不含密码），用于判断两个账号是否共用出口。"""
    if not proxy:
        return ""
    server = str(proxy.get("server") or "").strip().lower()
    username = str(proxy.get("username") or "").strip().lower()
    if not server:
        return ""
    return f"{server}|{username}" if username else server


def assign_proxy_from_pool(
    account_id: int,
    pool: list[dict[str, str]] | None = None,
    taken: set[str] | None = None,
) -> dict[str, str]:
    """从代理池给一个账号分配出口。

    ``taken`` 是已经被别的账号占用的代理标识。分配规则：

    1. 优先挑一个还没被占用的代理；
    2. 池子不够用时返回空字典（调用方应提示"共用本机 IP"）。
    """
    entries = pool if pool is not None else load_proxy_pool()
    used = taken if taken is not None else set()
    for entry in entries:
        key = proxy_key(entry)
        if key and key not in used:
            return dict(entry)
    return {}


def proxy_usage_map(
    bindings: dict[int, dict[str, str]],
) -> dict[str, list[int]]:
    """代理标识 -> 使用它的账号 id 列表。"""
    usage: dict[str, list[int]] = {}
    for account_id, proxy in (bindings or {}).items():
        key = proxy_key(proxy)
        if not key:
            continue
        usage.setdefault(key, []).append(int(account_id))
    return usage


def shared_proxy_warnings(
    bindings: dict[int, dict[str, str]],
    names: dict[int, str] | None = None,
) -> list[dict]:
    """找出被多个账号共用的代理出口，返回可展示的告警列表。"""
    usage = proxy_usage_map(bindings)
    label = names or {}
    warnings = []
    for key, account_ids in usage.items():
        if len(account_ids) < 2:
            continue
        warnings.append({
            "proxy": key,
            "accountIds": sorted(account_ids),
            "accountNames": [
                label.get(account_id, f"账号{account_id}")
                for account_id in sorted(account_ids)
            ],
            "severity": "error",
            "message": (
                "代理 " + key + " 被 "
                + str(len(account_ids))
                + " 个账号共用，出口 IP 相同，存在关联风控风险"
            ),
        })
    return warnings


def no_proxy_warning(account_count: int) -> dict | None:
    """未配置任何代理时的提示（多个账号共用本机 IP）。"""
    if int(account_count or 0) < 2:
        return None
    return {
        "severity": "warning",
        "message": NO_PROXY_WARNING,
        "accountCount": int(account_count),
    }
