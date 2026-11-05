# -*- coding: utf-8 -*-
"""服务运行参数：监听地址、跨域来源与访问令牌。

默认只监听回环地址，避免同局域网的其他设备在无鉴权的情况下
打开工作台、使用本机已登录账号发内容或收发私信。
需要通过手机/另一台电脑访问时，显式设置 YUNCE_HOST=0.0.0.0，
并建议同时设置 YUNCE_ACCESS_TOKEN。

访问令牌分两级：

1. **全局令牌**（``YUNCE_ACCESS_TOKEN``）：一个人机共用的万能钥匙，
   等价于管理员权限；
2. **用户令牌**（``YUNCE_TOKENS_FILE`` 指向的 JSON 文件）：带用户名、角色
   和账号范围（scope），用于多人协作。

角色定义：

- ``admin``：全部权限；
- ``operator``：可以发布、发私信，但只能操作自己范围内的账号；
- ``viewer``：只读。

令牌本身不保存明文，文件里存 ``sha256(token)``；请求时用哈希比对。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5409
# 单个请求允许的最大体积（160MB），与 Nginx client_max_body_size 200m 匹配。
MAX_CONTENT_LENGTH = 160 * 1024 * 1024
DEFAULT_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

# 角色与其拥有的权限点。
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
KNOWN_ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)

# 权限点：读、写发布、写私信、账号管理、团队管理。
PERM_READ = "read"
PERM_PUBLISH = "publish"
PERM_MESSAGE = "message"
PERM_ACCOUNT = "account"
PERM_TEAM = "team"

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    ROLE_ADMIN: (PERM_READ, PERM_PUBLISH, PERM_MESSAGE, PERM_ACCOUNT, PERM_TEAM),
    ROLE_OPERATOR: (PERM_READ, PERM_PUBLISH, PERM_MESSAGE),
    ROLE_VIEWER: (PERM_READ,),
}


def _load_dotenv() -> None:
    """读取项目根目录 .env；已存在的系统环境变量优先级更高。"""
    path = Path(__file__).with_name(".env")
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _env_text(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _as_bool(value: str, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "on")


HOST = _env_text("YUNCE_HOST", DEFAULT_HOST) or DEFAULT_HOST

try:
    PORT = int(_env_text("YUNCE_PORT", str(DEFAULT_PORT)) or DEFAULT_PORT)
except ValueError:
    PORT = DEFAULT_PORT

# 留空表示不校验令牌，仅适用于只监听回环地址的场景。
ACCESS_TOKEN = _env_text("YUNCE_ACCESS_TOKEN")

# 用户令牌文件（JSON）。格式见 ``load_user_tokens``。
TOKENS_FILE = _env_text("YUNCE_TOKENS_FILE")

# 非回环地址必须显式打开远程访问，并且必须设置访问令牌。
ALLOW_REMOTE = _as_bool(_env_text("YUNCE_ALLOW_REMOTE"))

# 默认只允许本机前端开发服务器跨域；需要额外来源时用逗号分隔配置。
CORS_ORIGINS = _split_csv(_env_text("YUNCE_CORS_ORIGINS")) or list(DEFAULT_ORIGINS)

# 静态资源与健康检查不参与令牌校验。
PUBLIC_PATHS = frozenset({"/health", "/favicon.ico", "/vite.svg"})
PUBLIC_PREFIXES = ("/assets/",)


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path == "/":
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def extract_token(headers, args) -> str:
    """从请求头或查询参数中取出访问令牌。"""
    for key in ("X-YunCe-Token", "X-Yunce-Token"):
        value = headers.get(key)
        if value:
            return str(value).strip()
    auth = str(headers.get("Authorization") or "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(args.get("token") or "").strip()


# ---------------------------------------------------------------- 用户令牌

def hash_token(token: str) -> str:
    """令牌哈希：文件里只保存哈希，明文不落盘。"""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _read_tokens_file(path: str) -> list[dict]:
    target = Path(path)
    if not target.is_file():
        return []
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("tokens") or payload.get("users") or []
    if not isinstance(payload, list):
        return []
    entries = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        digest = str(
            item.get("token_hash") or item.get("hash") or ""
        ).strip().lower()
        if not digest:
            plain = str(item.get("token") or "").strip()
            if not plain:
                continue
            digest = hash_token(plain)
        role = str(item.get("role") or ROLE_VIEWER).strip().lower()
        if role not in KNOWN_ROLES:
            role = ROLE_VIEWER
        scope = item.get("scope")
        if scope is None:
            scope = item.get("accounts") or []
        if isinstance(scope, (str, int)):
            scope = [str(scope)]
        if not isinstance(scope, list):
            scope = []
        entries.append({
            "user": str(item.get("user") or item.get("name") or "").strip(),
            "role": role,
            "scope": [str(value).strip() for value in scope if str(value).strip()],
            "token_hash": digest,
            "disabled": bool(item.get("disabled")),
        })
    return entries


def load_user_tokens(path: str = "") -> list[dict]:
    """读取用户令牌表。"""
    return _read_tokens_file(path or TOKENS_FILE)


def authenticate(candidate: str) -> dict | None:
    """用令牌换取身份信息。

    返回 ``{"user": ..., "role": ..., "scope": [...], "permissions": [...]}``，
    没有匹配时返回 ``None``。全局令牌返回 admin 身份。
    """
    candidate = str(candidate or "").strip()
    if not candidate:
        return None
    if ACCESS_TOKEN and hmac.compare_digest(candidate, ACCESS_TOKEN):
        return {
            "user": "local-admin",
            "role": ROLE_ADMIN,
            "scope": [],
            "permissions": list(ROLE_PERMISSIONS[ROLE_ADMIN]),
            "source": "global-token",
        }
    digest = hash_token(candidate)
    for entry in load_user_tokens():
        if entry.get("disabled"):
            continue
        if not hmac.compare_digest(str(entry.get("token_hash") or ""), digest):
            continue
        role = entry.get("role") or ROLE_VIEWER
        return {
            "user": entry.get("user") or "",
            "role": role,
            "scope": list(entry.get("scope") or []),
            "permissions": list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS[ROLE_VIEWER])),
            "source": "user-token",
        }
    return None


def has_permission(identity: dict | None, permission: str) -> bool:
    """身份是否拥有某个权限点。"""
    if not identity:
        return False
    return str(permission or "") in set(identity.get("permissions") or [])


def in_scope(identity: dict | None, account_id: Any = "", account_name: str = "") -> bool:
    """账号是否在当前身份的范围内。

    scope 为空表示不限制（管理员）。
    """
    if not identity:
        return False
    scope = [str(value) for value in (identity.get("scope") or [])]
    if not scope:
        return True
    if account_id is not None and str(account_id) != "":
        if str(account_id) in scope:
            return True
    if account_name and str(account_name) in scope:
        return True
    return False


def scope_filter(identity: dict | None) -> list[str]:
    """返回身份可访问的账号 ID 列表（空 = 不限制）。"""
    if not identity:
        return []
    return [str(value) for value in (identity.get("scope") or [])]


# ---------------------------------------------------------------- 旧接口

def token_ok(candidate: str) -> bool:
    """令牌是否有效（全局令牌或用户令牌都行）。"""
    if not ACCESS_TOKEN and not TOKENS_FILE:
        return True
    return authenticate(candidate) is not None


def access_allowed(method: str, path: str, headers, args) -> bool:
    """判断一次请求是否可以通过访问令牌校验。"""
    if not ACCESS_TOKEN and not TOKENS_FILE:
        return True
    if str(method or "").upper() == "OPTIONS" or is_public_path(path):
        return True
    return token_ok(extract_token(headers, args))


def is_loopback_host(host: str) -> bool:
    return str(host or "").strip().lower() in ("127.0.0.1", "localhost", "::1")


def validate_server_config() -> None:
    """阻止“监听全网卡但零鉴权”的危险组合。"""
    if is_loopback_host(HOST):
        return
    if not ALLOW_REMOTE:
        raise RuntimeError(
            "检测到 YUNCE_HOST 监听非回环地址，但未设置 YUNCE_ALLOW_REMOTE=1。"
            "如确需局域网访问，请显式开启远程访问并设置 YUNCE_ACCESS_TOKEN。"
        )
    if not ACCESS_TOKEN and not TOKENS_FILE:
        raise RuntimeError(
            "远程访问已开启，但未设置 YUNCE_ACCESS_TOKEN 或 YUNCE_TOKENS_FILE。"
            "零鉴权的局域网工作台会暴露账号 Cookie 和私信，已拒绝启动。"
        )
