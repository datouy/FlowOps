# -*- coding: utf-8 -*-
"""Account-scoped persistent browser environments.

One platform account must keep using one Chromium user-data directory, one
fingerprint and one proxy.  Cookie snapshots remain useful for backup and for
migrating older accounts, but they are no longer the only browser identity.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger


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
    result = {
        "profileKey": current["profileKey"] or _safe_key(str(payload.get("profileKey") or "")),
        "proxy": current["proxy"],
    }
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
        "proxyEnabled": bool(proxy.get("server")),
        "proxyServer": proxy.get("server") or "",
        "proxyUsername": proxy.get("username") or "",
        "proxyConfigured": bool(proxy.get("server")),
    }


def update_proxy(
    cookie_path: str | Path,
    account_id: int | None = None,
    *,
    server: str = "",
    username: str = "",
    password: str | None = None,
) -> dict[str, Any]:
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
