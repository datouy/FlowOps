# -*- coding: utf-8 -*-
"""令牌身份（角色 / 权限点 / 账号范围）在接口层的真正落地。

``runtime_config.in_scope`` 与 ``has_permission`` 早就能回答"这个令牌能不能
操作这个账号"，但后端路由从来没调用过它们——带 scope 的运营令牌照样能看到
全部账号、全部发布任务和全部私信会话，多人协作等于没做。

这个模块把两个原语收敛成路由可以直接调用的助手：

* :func:`current_identity` —— 当前请求的身份。**未启用访问令牌**时返回
  ``None``（本机单用户模式），此时不做任何限制，行为与改造前一致。
* :func:`has_permission` —— 权限点判断（未启用鉴权时一律放行）。
* :func:`account_in_scope` —— 账号是否在令牌范围内（id / 账号名 / Cookie 文件名
  三种标识都认）。
* :func:`filter_accounts` / :func:`filter_rows_by_scope` —— 按范围过滤列表。
* :func:`filter_jobs` —— 按范围过滤发布任务与发布记录（它们只记账号文件名，
  需要先还原成账号才能判断）。
* :func:`filter_account_pairs` —— 按范围过滤私信侧的 ``(平台, 账号名)`` 组合。

约定：范围受限且目标不在范围内时一律**拒绝**（403），而不是静默返回空
列表——让越权行为可被发现，而不是被当成"没有数据"。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from flask import g, has_app_context, jsonify

import conf
import runtime_config
from db_utils import connect

# 账号表 -> 平台中文名。私信侧用平台中文名标识账号，这里保持一致。
PLATFORM_LABELS = {
    1: "小红书", 2: "视频号", 3: "抖音",
    4: "快手", 5: "微博", 6: "百家号",
}

# 账号行里可能出现的标识字段。
_ID_KEYS = ("id", "account_id")
_NAME_KEYS = ("name", "userName", "account_name")
_FILE_KEYS = ("filePath", "cookie_path", "cookie")


# ---------------------------------------------------------------- 身份

def current_identity() -> dict | None:
    """当前请求的身份；未启用鉴权或不在请求上下文里时返回 None。"""
    if not has_app_context():
        return None
    return getattr(g, "identity", None)


def scope_entries() -> list[str]:
    """当前身份的账号范围；空列表表示不限制（管理员或未启用鉴权）。"""
    identity = current_identity()
    if not identity:
        return []
    return [
        str(value).strip()
        for value in runtime_config.scope_filter(identity)
        if str(value).strip()
    ]


def unrestricted() -> bool:
    """True 表示可以访问全部账号。"""
    return not scope_entries()


def has_permission(permission: str) -> bool:
    """权限点判断；未启用鉴权时一律放行。"""
    identity = current_identity()
    if identity is None:
        return True
    return runtime_config.has_permission(identity, permission)


def forbidden(message: str = "无权访问该资源"):
    """统一的 403 响应。"""
    return jsonify({"code": 403, "msg": message, "data": None}), 403


def require_permission(permission: str, message: str = "") -> Any:
    """缺少权限点时返回 403 响应；有权限时返回 None。"""
    if has_permission(permission):
        return None
    return forbidden(message or f"当前身份没有「{permission}」权限")


# ---------------------------------------------------------------- 账号范围

# 账号表所在根目录。默认用 ``conf.BASE_DIR``；后端启动时会改成自己的
# ``BASE_DIR``（测试里两者可能被替换成不同目录，必须指向同一个库）。
_BASE_DIR_PROVIDER = None


def bind_base_dir(provider) -> None:
    """绑定账号表根目录的解析函数（后端在启动时调用）。

    用函数而不是值，是为了让测试里 ``monkeypatch.setattr(be, "BASE_DIR", tmp)``
    之后仍然指向同一个临时数据库——否则 scope 判定和路由读的是两个不同的库。
    """
    global _BASE_DIR_PROVIDER
    _BASE_DIR_PROVIDER = provider


def _base_dir() -> Path:
    if _BASE_DIR_PROVIDER is not None:
        try:
            return Path(_BASE_DIR_PROVIDER())
        except Exception:
            pass
    return Path(conf.BASE_DIR)


def _account_table() -> list[dict]:
    """账号表的最小视图：id / 平台 / 名称 / Cookie 文件名。"""
    path = _base_dir() / "db" / "database.db"
    if not path.is_file():
        return []
    try:
        with connect(path, timeout=10) as conn:
            rows = conn.execute(
                "SELECT id, type, userName, filePath FROM user_info ORDER BY id"
            ).fetchall()
    except Exception:
        return []
    return [dict(row) for row in rows]


def _allowed() -> tuple[set[str], set[str], set[str]] | None:
    """解析当前令牌可访问的账号标识集合。

    返回 ``(账号 id, 账号名, Cookie 文件名)`` 三个集合；``None`` 表示不限制。
    令牌里的 scope 可以写账号 id，也可以写账号名或 Cookie 文件名，这里统一
    展开成三套标识，便于按任意一种标识过滤数据。
    """
    identity = current_identity()
    if identity is None:
        return None
    scope = scope_entries()
    if not scope:
        return None
    ids = set(scope)
    names = set(scope)
    files = set(scope)
    for row in _account_table():
        row_id = str(row.get("id") or "")
        row_name = str(row.get("userName") or "").strip()
        row_file = str(row.get("filePath") or "").strip()
        matched = (
            (row_id and row_id in scope)
            or (row_name and row_name in scope)
            or (row_file and (row_file in scope or Path(row_file).name in scope))
        )
        if not matched:
            continue
        if row_id:
            ids.add(row_id)
        if row_name:
            names.add(row_name)
        if row_file:
            files.add(row_file)
            files.add(Path(row_file).name)
    return ids, names, files


def account_exists(account_id: Any) -> bool:
    """账号是否真实存在（不区分是否在范围内）。"""
    try:
        value = int(account_id)
    except (TypeError, ValueError):
        return False
    return any(int(row.get("id") or 0) == value for row in _account_table())


def account_in_scope(
    account_id: Any = None,
    account_name: str = "",
    cookie_file: str = "",
) -> bool:
    """账号是否在当前令牌的范围内。"""
    identity = current_identity()
    if identity is None:
        return True
    scope = scope_entries()
    if not scope:
        return True
    if runtime_config.in_scope(
        identity,
        account_id="" if account_id is None else account_id,
        account_name=account_name or "",
    ):
        return True
    allowed = _allowed()
    if allowed is None:
        return True
    ids, names, files = allowed
    if account_id is not None and str(account_id) in ids:
        return True
    if account_name and str(account_name).strip() in names:
        return True
    if cookie_file:
        value = str(cookie_file).strip()
        if value in files or Path(value).name in files:
            return True
    return False


def _candidates(row: Any) -> tuple[set[str], set[str], set[str]]:
    """从一行数据里抽出账号 id / 账号名 / Cookie 文件名候选。"""
    ids: set[str] = set()
    names: set[str] = set()
    files: set[str] = set()
    if not isinstance(row, dict):
        return ids, names, files
    for key in _ID_KEYS:
        value = row.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, str)) and str(value) != "":
            ids.add(str(value))
    for key in _NAME_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
    for key in _FILE_KEYS:
        value = row.get(key)
        if isinstance(value, dict):
            value = value.get("name")
        if isinstance(value, str) and value.strip():
            files.add(value.strip())
            files.add(Path(value).name)
    return ids, names, files


def filter_rows_by_scope(rows: Iterable[Any]) -> list[Any]:
    """按账号范围过滤一批数据行。"""
    allowed = _allowed()
    if allowed is None:
        return list(rows)
    ids, names, files = allowed
    result = []
    for row in rows or []:
        row_ids, row_names, row_files = _candidates(row)
        if (row_ids & ids) or (row_names & names) or (row_files & files):
            result.append(row)
    return result


def filter_accounts(rows: Iterable[Any]) -> list[Any]:
    """按账号范围过滤账号列表（账号管理接口用）。"""
    return filter_rows_by_scope(rows)


def filter_legacy_rows(rows: Iterable[Any]) -> list[Any]:
    """按账号范围过滤老接口的 5 列账号行。

    老结构是 ``[id, type, filePath, userName, status]`` 的列表，不是字典。
    """
    allowed = _allowed()
    if allowed is None:
        return list(rows)
    ids, names, files = allowed
    result = []
    for row in rows or []:
        try:
            account_id = str(row[0])
            cookie_file = str(row[2] or "")
            account_name = str(row[3] or "").strip()
        except (TypeError, IndexError):
            continue
        candidates = {account_id, account_name, cookie_file}
        if cookie_file:
            candidates.add(Path(cookie_file).name)
        if candidates & (ids | names | files):
            result.append(row)
    return result


def filter_auth_sessions(rows: Iterable[Any]) -> list[Any]:
    """按账号范围过滤授权会话。

    会话行里的 ``id`` 是会话主键而不是账号 id，所以不能直接用
    :func:`filter_rows_by_scope`（它会把会话 id 当成账号 id 比对）。
    """
    allowed = _allowed()
    if allowed is None:
        return list(rows)
    ids, names, files = allowed
    result = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        account_id = str(row.get("account_id") or "").strip()
        account_name = str(row.get("account_name") or "").strip()
        cookie_file = str(row.get("cookie_name") or "").strip()
        candidates = {account_id, account_name, cookie_file}
        if cookie_file:
            candidates.add(Path(cookie_file).name)
        if candidates & (ids | names | files):
            result.append(row)
    return result


def filter_publish_items(rows: Iterable[Any]) -> list[Any]:
    """按账号范围过滤发布子任务（``publish_items``）。

    子任务行里的 ``id`` 是子任务主键，不是账号 id——不能直接用
    :func:`filter_rows_by_scope`，否则子任务 id 恰好等于某个范围外账号 id
    时会造成误放行。
    """
    allowed = _allowed()
    if allowed is None:
        return list(rows)
    ids, names, files = allowed
    result = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        account_id = str(row.get("account_id") or "").strip()
        account_name = str(row.get("account_name") or "").strip()
        cookie_file = str(row.get("cookie") or "").strip()
        candidates = {account_id, account_name, cookie_file}
        if cookie_file:
            candidates.add(Path(cookie_file).name)
        if candidates & (ids | names | files):
            result.append(row)
    return result


# ---------------------------------------------------------------- 私信侧

def allowed_account_names() -> set[str] | None:
    """令牌可访问的账号名集合；``None`` 表示不限制。"""
    allowed = _allowed()
    if allowed is None:
        return None
    return allowed[1]


def filter_account_pairs(
    pairs: Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    """按账号范围过滤私信侧的 ``(平台, 账号名)`` 组合。"""
    names = allowed_account_names()
    if names is None:
        return list(pairs)
    return [
        pair for pair in pairs or []
        if str(pair[1] or "").strip() in names
    ]


def dm_account_in_scope(platform: str, account: str) -> bool:
    """私信侧的账号是否在令牌范围内。"""
    identity = current_identity()
    if identity is None:
        return True
    scope = scope_entries()
    if not scope:
        return True
    name = str(account or "").strip()
    if not name:
        return False
    if runtime_config.in_scope(identity, account_name=name):
        return True
    names = allowed_account_names()
    if names is None:
        return True
    return name in names


# ---------------------------------------------------------------- 发布任务

def _resolve_account_refs(refs: Sequence[Any]) -> list[dict]:
    """把账号引用（id / 账号名 / Cookie 文件名）还原成账号行。"""
    table = _account_table()
    resolved: list[dict] = []
    for ref in refs or []:
        value = str(ref or "").strip()
        if not value:
            continue
        for row in table:
            row_id = str(row.get("id") or "")
            row_name = str(row.get("userName") or "").strip()
            row_file = str(row.get("filePath") or "").strip()
            if (
                (row_id and row_id == value)
                or (row_name and row_name == value)
                or (row_file and (row_file == value or Path(row_file).name == value))
            ):
                if row not in resolved:
                    resolved.append(row)
                break
    return resolved


def _job_refs(row: Any) -> list[str]:
    """发布任务 / 发布记录里记的账号引用。"""
    if not isinstance(row, dict):
        return []
    raw = row.get("account_list")
    if raw in (None, ""):
        raw = row.get("accounts")
    if raw in (None, ""):
        raw = row.get("account")
    if raw in (None, ""):
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if text.startswith("["):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = []
        if isinstance(payload, list):
            return [str(item).strip() for item in payload if str(item).strip()]
    return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]


def filter_jobs(rows: Iterable[Any]) -> list[Any]:
    """按账号范围过滤发布任务 / 发布记录。

    一个任务可能同时包含范围内外的账号：只要有一个账号越界，整个任务就不
    展示——否则会把别人的账号名和素材一起泄露出去。
    """
    allowed = _allowed()
    if allowed is None:
        return list(rows)
    ids, names, files = allowed
    result = []
    for row in rows or []:
        refs = _job_refs(row)
        if not refs:
            result.append(row)
            continue
        targets = _resolve_account_refs(refs)
        if not targets:
            # 账号已被删除：范围外的账号同样不可见。
            continue
        ok = True
        for target in targets:
            target_ids = {str(target.get("id") or "")}
            target_names = {str(target.get("userName") or "").strip()}
            target_files = {str(target.get("filePath") or "").strip()}
            if target_files:
                target_files.add(Path(str(target["filePath"])).name)
            if not (
                (target_ids & ids)
                or (target_names & names)
                or (target_files & files)
            ):
                ok = False
                break
        if ok:
            result.append(row)
    return result


def job_in_scope(row: Any) -> bool:
    """单个发布任务 / 发布记录是否完全落在账号范围内。"""
    return bool(filter_jobs([row]))
