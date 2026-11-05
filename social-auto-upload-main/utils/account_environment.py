# -*- coding: utf-8 -*-
"""Account-scoped persistent browser environments.

One platform account must keep using one Chromium user-data directory, one
fingerprint and one proxy.  Cookie snapshots remain useful for backup and for
migrating older accounts, but they are no longer the only browser identity.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from conf import BASE_DIR


ENV_DIR_NAME = "account_environments"
PROFILE_DIR_NAME = "browser_profiles"
SEED_MARKER = ".flowops-storage-state-seeded"


def _cookie_root(cookie_path: str | Path) -> Path:
    path = Path(cookie_path).expanduser().resolve()
    return path.parent


def _safe_key(value: str, limit: int = 64) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-.")
    if not text:
        return ""
    return text[:limit]


def _normalize_proxy(proxy: Any) -> dict[str, str]:
    """Normalize a proxy binding; invalid schemes are dropped."""
    if not isinstance(proxy, dict):
        return {"server": "", "username": "", "password": ""}
    server = str(proxy.get("server") or "").strip()
    if server and not re.match(r"^(https?|socks5)://[^\s]+", server, re.I):
        return {"server": "", "username": "", "password": ""}
    return {
        "server": server,
        "username": str(proxy.get("username") or "").strip(),
        "password": str(proxy.get("password") or ""),
    }


def environment_path(cookie_path: str | Path) -> Path:
    """Return the sidecar storing non-secret profile/proxy configuration."""
    path = Path(cookie_path).expanduser().resolve()
    key = _safe_key(path.stem)
    return _cookie_root(path) / ENV_DIR_NAME / f"{key}.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_environment(cookie_path: str | Path) -> dict[str, Any]:
    path = environment_path(cookie_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    proxy = payload.get("proxy") if isinstance(payload.get("proxy"), dict) else {}
    return {
        "profileKey": _safe_key(str(payload.get("profileKey") or "")),
        "accountId": int(payload.get("accountId") or 0),
        "loginSessionId": str(payload.get("loginSessionId") or ""),
        "loginSourceProfileKey": _safe_key(
            str(payload.get("loginSourceProfileKey") or "")
        ),
        "isolationVersion": int(payload.get("isolationVersion") or 0),
        "proxy": {
            "server": str(proxy.get("server") or "").strip(),
            "username": str(proxy.get("username") or "").strip(),
            "password": str(proxy.get("password") or ""),
        },
    }


def save_environment(cookie_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist profile/proxy values; proxy passwords never enter API responses."""
    current = load_environment(cookie_path)
    proxy = payload.get("proxy") if isinstance(payload.get("proxy"), dict) else None
    result = dict(current)
    result["profileKey"] = (
        current["profileKey"]
        or _safe_key(str(payload.get("profileKey") or ""))
    )
    result["proxy"] = dict(current["proxy"])
    for key in (
        "accountId", "loginSessionId", "loginSourceProfileKey",
        "isolationVersion",
    ):
        if key in payload:
            result[key] = payload[key]
    if proxy is not None:
        server = str(proxy.get("server") or "").strip()
        if server and not re.match(r"^(https?|socks5)://[^\s]+", server, re.I):
            raise ValueError("代理地址必须使用 http://、https:// 或 socks5://")
        result["proxy"] = {
            "server": server,
            "username": str(proxy.get("username") or "").strip(),
            "password": str(proxy.get("password") or ""),
        }
    _atomic_json(environment_path(cookie_path), result)
    return result


def profile_dir(cookie_path: str | Path, account_id: int | None = None) -> Path:
    env = load_environment(cookie_path)
    key = env["profileKey"]
    if not key:
        key = f"account-{int(account_id)}" if account_id else f"cookie-{Path(cookie_path).stem}"
        env["profileKey"] = _safe_key(key)
        _atomic_json(environment_path(cookie_path), env)
    return _cookie_root(cookie_path) / PROFILE_DIR_NAME / f"auth-{env['profileKey']}"


def account_id_for_cookie(cookie_path: str | Path) -> int | None:
    """Resolve the stable account id for a stored credential file."""
    path = Path(cookie_path).expanduser().resolve()
    database = path.parent.parent / "db" / "database.db"
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(database) as conn:
            row = conn.execute(
                "SELECT id FROM user_info WHERE filePath=? ORDER BY id LIMIT 1",
                (path.name,),
            ).fetchone()
        return int(row[0]) if row else None
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None


def associate_account(cookie_path: str | Path, account_id: int) -> None:
    env = load_environment(cookie_path)
    # Preserve a profile created during the first QR login.  Re-authenticating
    # the account later must continue from that same Chromium user-data dir.
    if not env["profileKey"]:
        env["profileKey"] = _safe_key(f"account-{int(account_id)}")
    _atomic_json(environment_path(cookie_path), env)


def bind_environment(
    source_cookie: str | Path,
    target_cookie: str | Path,
    account_id: int,
) -> None:
    """Carry the login-time profile/proxy mapping to a newly saved account."""
    env = load_environment(source_cookie)
    if not env.get("profileKey"):
        env["profileKey"] = _safe_key(f"account-{int(account_id)}")
    _atomic_json(environment_path(target_cookie), env)


def _rewrite_storage_device_id(cookie_path: str | Path, device_id: str) -> None:
    """Give each isolated account credential its own persisted device signal."""
    path = Path(cookie_path).expanduser().resolve()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(state, dict):
        return
    changed = False
    for origin in state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        values = origin.get("localStorage")
        if not isinstance(values, list):
            continue
        found = False
        for item in values:
            if isinstance(item, dict) and item.get("name") == "_finger_print_device_id":
                item["value"] = device_id
                found = True
                changed = True
        if not found:
            values.append({"name": "_finger_print_device_id", "value": device_id})
            changed = True
    if changed:
        _atomic_json(path, state)


def bind_tencent_channel_environment(
    source_cookie: str | Path,
    target_cookie: str | Path,
    account_id: int,
    identity: str,
    *,
    login_session_id: str = "",
    browser_version: str = "131",
    proxy: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a durable per-Channels environment after one WeChat login.

    A single WeChat administrator login can discover several Channels.  The
    login window is only a discovery session: each discovered channel gets its
    own profile key, fingerprint and proxy sidecar.  The source profile and
    login session id are retained as metadata so the first WeChat login can
    still be traced without sharing the runtime browser environment.

    ``proxy`` is the **channel-level** proxy binding.  It is deliberately not
    copied from the login session: several channels discovered by the same
    administrator must not share one egress IP.  When no explicit proxy is
    given the channel keeps whatever it already had (e.g. an earlier manual
    assignment) and the caller is expected to surface the "shared local IP"
    risk instead of silently duplicating the login proxy.
    """
    from utils.account_fingerprint import new_fingerprint, save_fingerprint

    source = load_environment(source_cookie)
    current = load_environment(target_cookie)
    already_isolated = (
        current.get("isolationVersion", 0) >= 2
        and int(current.get("accountId") or 0) == int(account_id)
    )
    if already_isolated:
        result = current
    else:
        identity_digest = hashlib.sha256(
            f"{account_id}:{identity}".encode("utf-8")
        ).hexdigest()[:12]
        result = {
            "profileKey": _safe_key(f"tencent-{int(account_id)}-{identity_digest}"),
            "accountId": int(account_id),
            "loginSessionId": str(login_session_id or ""),
            "loginSourceProfileKey": source.get("profileKey") or "",
            "isolationVersion": 2,
            # 一频道一出口：不继承登录会话的代理。
            "proxy": _normalize_proxy(proxy),
        }
        _atomic_json(environment_path(target_cookie), result)

    fingerprint = None
    try:
        from utils.account_fingerprint import load_fingerprint

        fingerprint = load_fingerprint(target_cookie)
    except Exception:
        fingerprint = None
    if fingerprint is None:
        seed = f"tencent-channel:{int(account_id)}:{identity or target_cookie}"
        fingerprint = new_fingerprint(seed, browser_version)
        save_fingerprint(target_cookie, fingerprint)
    _rewrite_storage_device_id(target_cookie, str(fingerprint.get("device_id") or ""))
    return result


def proxy_settings(cookie_path: str | Path) -> dict[str, str] | None:
    proxy = load_environment(cookie_path)["proxy"]
    server = str(proxy.get("server") or "").strip()
    if not server:
        return None
    settings = {"server": server}
    if proxy.get("username"):
        settings["username"] = str(proxy["username"])
    if proxy.get("password"):
        settings["password"] = str(proxy["password"])
    return settings


def public_environment(cookie_path: str | Path, account_id: int | None = None) -> dict[str, Any]:
    env = load_environment(cookie_path)
    target = profile_dir(cookie_path, account_id)
    proxy = env["proxy"]
    return {
        "profileKey": target.name.removeprefix("auth-"),
        "profileExists": target.is_dir(),
        "persistent": True,
        "loginSessionId": env.get("loginSessionId") or "",
        "loginSourceProfileKey": env.get("loginSourceProfileKey") or "",
        "isolationVersion": int(env.get("isolationVersion") or 0),
        "proxyEnabled": bool(proxy.get("server")),
        "proxyServer": proxy.get("server") or "",
        "proxyUsername": proxy.get("username") or "",
        "proxyConfigured": bool(proxy.get("server")),
        # 代理绑定状态：是否与其它账号共用同一个出口。
        "proxyBinding": proxy_binding_status(cookie_path, account_id),
    }


def proxy_binding_status(
    cookie_path: str | Path, account_id: int | None = None,
) -> dict[str, Any]:
    """Report whether this account's proxy is shared with other accounts."""
    from utils.proxy_pool import proxy_key, shared_proxy_warnings

    env = load_environment(cookie_path)
    key = proxy_key(env.get("proxy"))
    if not key:
        return {
            "server": "",
            "configured": False,
            "shared": False,
            "sharedWith": [],
            "message": "未配置代理",
        }
    bindings, names = _all_proxy_bindings()
    shared = [item for item in shared_proxy_warnings(bindings, names)
              if key in item["proxy"]]
    shared_ids = shared[0]["accountIds"] if shared else []
    return {
        "server": str(env["proxy"].get("server") or ""),
        "configured": True,
        "shared": bool(shared),
        "sharedWith": [
            {"id": account_id_value,
             "name": names.get(account_id_value, f"账号{account_id_value}")}
            for account_id_value in shared_ids
            if account_id_value != int(account_id or 0)
        ],
        "message": (
            f"该代理被 {len(shared_ids)} 个账号共用，出口 IP 相同"
            if shared else "该账号使用独立代理出口"
        ),
    }


def _all_proxy_bindings() -> tuple[dict[int, dict[str, str]], dict[int, str]]:
    """Collect every account's proxy binding from the account database."""
    import sqlite3

    path = Path(cookie_path_root()).expanduser().resolve()
    database = path / "db" / "database.db"
    bindings: dict[int, dict[str, str]] = {}
    names: dict[int, str] = {}
    if not database.is_file():
        return bindings, names
    try:
        with sqlite3.connect(database) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, userName, filePath FROM user_info ORDER BY id"
            ).fetchall()
        for row in rows:
            cookie_file = str(row["filePath"] or "").strip()
            if not cookie_file:
                continue
            proxy = load_environment(
                path / "cookiesFile" / Path(cookie_file).name
            ).get("proxy") or {}
            if not proxy.get("server"):
                continue
            bindings[int(row["id"])] = dict(proxy)
            names[int(row["id"])] = str(row["userName"] or "")
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return bindings, names
    return bindings, names


def cookie_path_root() -> str:
    """The directory that holds ``cookiesFile`` and ``db``."""
    return str(Path(BASE_DIR).expanduser().resolve())


def update_proxy(
    cookie_path: str | Path,
    account_id: int | None = None,
    *,
    server: str = "",
    username: str = "",
    password: str | None = None,
) -> dict[str, Any]:
    """Bind a proxy to one account.

    The binding is stored per account sidecar, so changing a Channels
    account's proxy never touches the sibling channels discovered by the same
    WeChat administrator login.
    """
    profile_dir(cookie_path, account_id)
    current = load_environment(cookie_path)["proxy"]
    save_environment(cookie_path, {
        "proxy": {
            "server": server,
            "username": username,
            "password": current.get("password", "") if password is None else password,
        },
    })
    return public_environment(cookie_path, account_id)


def assign_pool_proxy(
    cookie_path: str | Path,
    account_id: int | None = None,
    *,
    pool: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Give one account an unused proxy from the pool.

    Returns the public environment.  When the pool has no free proxy left the
    account keeps its current binding and the result carries a warning so the
    UI can tell the user that accounts share an egress IP.
    """
    from utils.proxy_pool import (
        assign_proxy_from_pool,
        load_proxy_pool,
        proxy_key,
        proxy_usage_map,
    )

    bindings, _names = _all_proxy_bindings()
    taken = set(proxy_usage_map(bindings))
    current = load_environment(cookie_path)["proxy"]
    current_key = proxy_key(current)
    # The account's own binding does not count as "taken".
    if current_key:
        taken.discard(current_key)
    assigned = assign_proxy_from_pool(
        int(account_id or 0), pool if pool is not None else load_proxy_pool(), taken,
    )
    if not assigned:
        return {
            **public_environment(cookie_path, account_id),
            "proxyAssigned": False,
            "proxyWarning": "代理池已用完，该账号未分配独立出口，可能与其他账号共用 IP",
        }
    update_proxy(
        cookie_path,
        account_id,
        server=assigned["server"],
        username=assigned.get("username", ""),
        password=assigned.get("password", ""),
    )
    return {
        **public_environment(cookie_path, account_id),
        "proxyAssigned": True,
        "proxyWarning": "",
    }


def _read_storage_state(cookie_path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(cookie_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _local_storage_script(state: dict[str, Any]) -> str:
    origins = {}
    for item in state.get("origins") or []:
        if not isinstance(item, dict) or not item.get("origin"):
            continue
        values = {}
        for entry in item.get("localStorage") or []:
            if isinstance(entry, dict) and entry.get("name"):
                values[str(entry["name"])] = str(entry.get("value") or "")
        if values:
            origins[str(item["origin"])] = values
    if not origins:
        return ""
    return f"""
(() => {{
  const origins = {json.dumps(origins, ensure_ascii=False)};
  const values = origins[location.origin];
  if (!values) return;
  for (const [key, value] of Object.entries(values)) {{
    try {{ localStorage.setItem(key, value); }} catch (_) {{}}
  }}
}})();
"""


def _mark_seeded(target: Path) -> None:
    try:
        (target / SEED_MARKER).write_text("1\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("账号持久目录写入迁移标记失败: {}", exc)


def hydrate_context(context, cookie_path: str | Path, account_id: int | None = None) -> None:
    """Import an existing storage_state into a new persistent profile once."""
    target = profile_dir(cookie_path, account_id)
    target.mkdir(parents=True, exist_ok=True)
    marker = target / SEED_MARKER
    if marker.exists():
        return
    state = _read_storage_state(cookie_path)
    cookies = [item for item in state.get("cookies") or [] if isinstance(item, dict)]
    if cookies:
        context.add_cookies(cookies)
    script = _local_storage_script(state)
    if script:
        context.add_init_script(script=script)
    _mark_seeded(target)


async def hydrate_context_async(context, cookie_path: str | Path, account_id: int | None = None) -> None:
    target = profile_dir(cookie_path, account_id)
    target.mkdir(parents=True, exist_ok=True)
    marker = target / SEED_MARKER
    if marker.exists():
        return
    state = _read_storage_state(cookie_path)
    cookies = [item for item in state.get("cookies") or [] if isinstance(item, dict)]
    if cookies:
        await context.add_cookies(cookies)
    script = _local_storage_script(state)
    if script:
        await context.add_init_script(script=script)
    _mark_seeded(target)


def remove_environment(cookie_path: str | Path, account_id: int | None = None) -> None:
    """Delete the persistent profile and its sidecar for a removed account."""
    target = profile_dir(cookie_path, account_id)
    root = _cookie_root(cookie_path) / PROFILE_DIR_NAME
    try:
        resolved = target.resolve()
        if resolved.is_relative_to(root.resolve()) and resolved != root.resolve():
            shutil.rmtree(resolved, ignore_errors=True)
    except (OSError, ValueError):
        pass
    try:
        environment_path(cookie_path).unlink(missing_ok=True)
    except OSError:
        pass
