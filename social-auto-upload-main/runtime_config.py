# -*- coding: utf-8 -*-
"""服务运行参数：监听地址、跨域来源与访问令牌。

默认只监听回环地址，避免同局域网的其他设备在无鉴权的情况下
打开工作台、使用本机已登录账号发内容或收发私信。
需要通过手机/另一台电脑访问时，显式设置 YUNCE_HOST=0.0.0.0，
并建议同时设置 YUNCE_ACCESS_TOKEN。

访问令牌分三级：

1. **全局令牌**（``YUNCE_ACCESS_TOKEN``）：一个人机共用的万能钥匙，
   等价于管理员权限；
2. **用户令牌**（``YUNCE_TOKENS_FILE`` 指向的 JSON 文件）：带用户名、角色
   和账号范围（scope），用于多人协作。
3. **实名会话**（``YUNCE_PERSONAL_AUTH=1``）：用户输入个人密码，服务端签发
   可过期、可撤销的会话；数据库只保存会话令牌哈希。

角色定义：

- ``admin``：全部权限；
- ``operator``：执行发布、操作私信；
- ``cutter``：编辑内容、维护素材；
- ``reviewer``：审核内容；
- ``viewer``：仅保留为迁移兼容身份，新部署不应继续分配。

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

# 角色与其拥有的权限点。四角色是面向客户的生产角色；viewer 仅用于旧令牌迁移。
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_CUTTER = "cutter"
ROLE_REVIEWER = "reviewer"
ROLE_VIEWER = "viewer"
PRODUCTION_ROLES = (ROLE_CUTTER, ROLE_REVIEWER, ROLE_OPERATOR, ROLE_ADMIN)
KNOWN_ROLES = (*PRODUCTION_ROLES, ROLE_VIEWER)

PERM_CONTENT_EDIT = "content.edit"
PERM_MATERIAL_WRITE = "material.write"
PERM_REVIEW_DECIDE = "review.decide"
PERM_PUBLISH_EXECUTE = "publish.execute"
PERM_MESSAGE_OPERATE = "message.operate"
PERM_ACCOUNT_MANAGE = "account.manage"
PERM_TEAM_MANAGE = "team.manage"
PERM_AUDIT_READ = "audit.read"

# 兼容旧代码中的常量名；新代码应使用明确的新名字。
PERM_READ = "read"
PERM_PUBLISH = PERM_PUBLISH_EXECUTE
PERM_MESSAGE = PERM_MESSAGE_OPERATE
PERM_ACCOUNT = PERM_ACCOUNT_MANAGE
PERM_TEAM = PERM_TEAM_MANAGE

ALL_PERMISSIONS = (
    PERM_CONTENT_EDIT,
    PERM_MATERIAL_WRITE,
    PERM_REVIEW_DECIDE,
    PERM_PUBLISH_EXECUTE,
    PERM_MESSAGE_OPERATE,
    PERM_ACCOUNT_MANAGE,
    PERM_TEAM_MANAGE,
    PERM_AUDIT_READ,
)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    ROLE_ADMIN: ALL_PERMISSIONS,
    ROLE_OPERATOR: (PERM_PUBLISH_EXECUTE, PERM_MESSAGE_OPERATE),
    ROLE_CUTTER: (PERM_CONTENT_EDIT, PERM_MATERIAL_WRITE),
    ROLE_REVIEWER: (PERM_REVIEW_DECIDE,),
    ROLE_VIEWER: (),
}

# 首版审核策略：cutter 必经审核；现有 operator 直发默认兼容，可由团队显式收紧。
REVIEW_MODE_CUTTER_REQUIRED = "cutter_required"
REVIEW_MODE_ALL_REQUIRED = "all_required"
REVIEW_MODES = (
    REVIEW_MODE_CUTTER_REQUIRED,
    REVIEW_MODE_ALL_REQUIRED,
)


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

# 实名密码登录。关闭时保持旧令牌模式；开启后仍保留全局令牌和用户令牌，
# 用于迁移期和应急引导管理员。
PERSONAL_AUTH = _as_bool(_env_text("YUNCE_PERSONAL_AUTH"))

REVIEW_MODE = _env_text("FLOWOPS_REVIEW_MODE", REVIEW_MODE_CUTTER_REQUIRED).lower()

# 非回环地址必须显式打开远程访问，并且必须设置访问令牌。
ALLOW_REMOTE = _as_bool(_env_text("YUNCE_ALLOW_REMOTE"))

# 默认只允许本机前端开发服务器跨域；需要额外来源时用逗号分隔配置。
CORS_ORIGINS = _split_csv(_env_text("YUNCE_CORS_ORIGINS")) or list(DEFAULT_ORIGINS)

# 静态资源与健康检查不参与令牌校验。
PUBLIC_PATHS = frozenset({
    "/health",
    "/health/ready",
    "/favicon.ico",
    "/vite.svg",
    "/api/v2/auth/config",
    "/v2/auth/config",
    "/api/v2/auth/login",
    "/v2/auth/login",
    "/api/v2/auth/mfa/verify",
    "/v2/auth/mfa/verify",
    # Webhook 使用独立密钥/HMAC，不能要求外部系统持有后台管理 Token。
    "/dm/inbound",
})
PUBLIC_PREFIXES = ("/assets/",)

# 发布执行期强制要求每个账号绑定独立代理出口。关闭时"无代理"只是提醒，
# 开启后多账号环境下没有代理会被发布预检拦下（fail-closed）。
REQUIRE_PROXY = _as_bool(_env_text("FLOWOPS_REQUIRE_PROXY"))

# 账号浏览器启动后做一次动态指纹自检，关键字段不一致就拒绝执行账号动作。
# 默认关闭：实测部分 Chromium 构建里 navigator.platform / hardwareConcurrency /
# screen 的注入并不稳定，默认硬拦会误伤正常账号。确认注入生效后再用
# FLOWOPS_FINGERPRINT_FAIL_CLOSED=1 打开。
FINGERPRINT_FAIL_CLOSED = _as_bool(_env_text("FLOWOPS_FINGERPRINT_FAIL_CLOSED"))


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


class TokenConfigError(ValueError):
    """用户令牌文件存在无法安全解释的配置。"""


def _scope_mode_for_role(role: str) -> str:
    return "all" if role == ROLE_ADMIN else "list"


def _read_tokens_file(path: str, *, strict: bool = False) -> list[dict]:
    """读取用户令牌文件。

    ``strict=True`` 用于启动校验：任何未知角色、空用户名、空 scope 或损坏
    文件都会抛错。运行期认证使用非 strict 模式，非法条目直接不可认证，绝不
    降级成 viewer。
    """
    target = Path(path)
    if not target.is_file():
        if strict:
            raise TokenConfigError(f"令牌文件不存在：{target}")
        return []
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        if strict:
            raise TokenConfigError(f"无法读取令牌文件：{target}") from exc
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        if strict:
            raise TokenConfigError(f"令牌文件不是合法 JSON：{target}") from exc
        return []
    if isinstance(payload, dict):
        payload = payload.get("tokens") or payload.get("users") or []
    if not isinstance(payload, list):
        if strict:
            raise TokenConfigError("令牌文件根节点必须是数组或包含 tokens/users 数组")
        return []
    entries = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            if strict:
                raise TokenConfigError(f"第 {index} 个令牌条目不是对象")
            continue
        digest = str(
            item.get("token_hash") or item.get("hash") or ""
        ).strip().lower()
        if not digest:
            plain = str(item.get("token") or "").strip()
            if not plain:
                if strict:
                    raise TokenConfigError(f"第 {index} 个令牌缺少 token_hash/hash/token")
                continue
            digest = hash_token(plain)
        role = str(item.get("role") or "").strip().lower()
        if role not in KNOWN_ROLES:
            if strict:
                raise TokenConfigError(
                    f"第 {index} 个令牌角色无效：{role or '<空>'}；"
                    f"允许角色为 {', '.join(PRODUCTION_ROLES)}"
                )
            continue
        user = str(item.get("user") or item.get("name") or "").strip()
        if not user:
            if strict:
                raise TokenConfigError(f"第 {index} 个令牌缺少 user/name")
            continue
        scope = item.get("scope")
        if scope is None:
            scope = item.get("accounts") or []
        if isinstance(scope, (str, int)):
            scope = [str(scope)]
        if not isinstance(scope, list):
            scope = []
        normalized_scope = [str(value).strip() for value in scope if str(value).strip()]
        if role != ROLE_ADMIN and not normalized_scope:
            if strict:
                raise TokenConfigError(
                    f"用户「{user}」角色为 {role}，但没有配置账号 scope；"
                    "非管理员空 scope 会形成全账号访问，已拒绝"
                )
            continue
        entries.append({
            "user": user,
            "role": role,
            "scope": normalized_scope,
            "scope_mode": _scope_mode_for_role(role),
            "token_hash": digest,
            "disabled": bool(item.get("disabled")),
        })
    return entries


def load_user_tokens(path: str = "", *, strict: bool = False) -> list[dict]:
    """读取用户令牌表。"""
    return _read_tokens_file(path or TOKENS_FILE, strict=strict)


def validate_user_tokens(path: str = "") -> None:
    """严格校验用户令牌文件；供服务启动时提前暴露权限配置错误。"""
    target = path or TOKENS_FILE
    if not target:
        return
    load_user_tokens(target, strict=True)


def authenticate(candidate: str) -> dict | None:
    """用令牌换取身份信息。

    返回 ``{"user": ..., "role": ..., "scope": [...], "permissions": [...]}``，
    没有匹配时返回 ``None``。全局令牌返回 admin 身份。
    """
    candidate = str(candidate or "").strip()
    if not candidate:
        return None
    if PERSONAL_AUTH:
        try:
            import user_auth

            identity = user_auth.authenticate_session(candidate)
            if identity is not None:
                return identity
        except Exception:
            # An unreadable session store must not disable break-glass tokens.
            pass
    if ACCESS_TOKEN and hmac.compare_digest(candidate, ACCESS_TOKEN):
        return {
            "user": "local-admin",
            "role": ROLE_ADMIN,
            "scope": [],
            "scope_mode": "all",
            "permissions": list(ROLE_PERMISSIONS[ROLE_ADMIN]),
            "source": "global-token",
        }
    digest = hash_token(candidate)
    for entry in load_user_tokens():
        if entry.get("disabled"):
            continue
        if not hmac.compare_digest(str(entry.get("token_hash") or ""), digest):
            continue
        role = entry.get("role") or ""
        permissions = ROLE_PERMISSIONS.get(role)
        if permissions is None:
            continue
        return {
            "user": entry.get("user") or "",
            "role": role,
            "scope": list(entry.get("scope") or []),
            "scope_mode": entry.get("scope_mode") or _scope_mode_for_role(role),
            "permissions": list(permissions),
            "source": "user-token",
        }
    return None


def has_permission(identity: dict | None, permission: str) -> bool:
    """身份是否拥有某个权限点。"""
    if not identity:
        return False
    return str(permission or "") in set(identity.get("permissions") or [])


def scope_is_unrestricted(identity: dict | None) -> bool:
    """只有管理员或显式 all 模式可以跨全部账号。"""
    if not identity:
        return False
    return identity.get("role") == ROLE_ADMIN or identity.get("scope_mode") == "all"


def in_scope(identity: dict | None, account_id: Any = "", account_name: str = "") -> bool:
    """账号是否在当前身份的范围内。

    只有管理员或显式 ``scope_mode=all`` 的身份不受限制；非管理员空 scope
    必须失败关闭。
    """
    if not identity:
        return False
    if scope_is_unrestricted(identity):
        return True
    scope = [str(value) for value in (identity.get("scope") or []) if str(value)]
    if not scope:
        return False
    if account_id is not None and str(account_id) != "":
        if str(account_id) in scope:
            return True
    if account_name and str(account_name) in scope:
        return True
    return False


def scope_filter(identity: dict | None) -> list[str]:
    """返回身份可访问的账号 ID 列表（管理员 all 模式才表示不限制）。"""
    if not identity:
        return []
    return [str(value) for value in (identity.get("scope") or [])]


# ---------------------------------------------------------------- 旧接口

def token_ok(candidate: str) -> bool:
    """令牌是否有效（全局令牌或用户令牌都行）。"""
    if not ACCESS_TOKEN and not TOKENS_FILE and not PERSONAL_AUTH:
        return True
    return authenticate(candidate) is not None


def access_allowed(method: str, path: str, headers, args) -> bool:
    """判断一次请求是否可以通过访问令牌校验。"""
    if not ACCESS_TOKEN and not TOKENS_FILE and not PERSONAL_AUTH:
        return True
    if str(method or "").upper() == "OPTIONS" or is_public_path(path):
        return True
    return token_ok(extract_token(headers, args))


def is_loopback_host(host: str) -> bool:
    return str(host or "").strip().lower() in ("127.0.0.1", "localhost", "::1")


def authorization_status() -> dict:
    """返回真实生效的令牌角色/权限状态，不把数据库占位表当成鉴权事实。"""
    auth_configured = bool(ACCESS_TOKEN or TOKENS_FILE or PERSONAL_AUTH)
    token_error = ""
    try:
        validate_user_tokens()
    except TokenConfigError as exc:
        token_error = str(exc)
    personal_auth = {}
    if PERSONAL_AUTH:
        try:
            import user_auth

            personal_auth = user_auth.auth_status()
        except Exception as exc:
            token_error = token_error or f"实名认证状态不可读：{exc}"
    return {
        "ready": bool(auth_configured and not token_error and REVIEW_MODE in REVIEW_MODES),
        "authConfigured": auth_configured,
        "tokenConfigValid": not bool(token_error),
        "tokenError": token_error,
        "source": "runtime-token-map",
        "personalAuth": personal_auth,
        "roles": list(PRODUCTION_ROLES),
        "permissions": list(ALL_PERMISSIONS),
        "permissionsByRole": {
            role: list(ROLE_PERMISSIONS[role]) for role in PRODUCTION_ROLES
        },
        "reviewMode": REVIEW_MODE,
        "roleRevocationPolicy": "new requests denied immediately; queued tasks continue unless an administrator cancels them",
    }


def validate_server_config() -> None:
    """阻止危险鉴权组合，并严格校验用户令牌文件。"""
    if REVIEW_MODE not in REVIEW_MODES:
        raise RuntimeError(
            f"FLOWOPS_REVIEW_MODE 无效：{REVIEW_MODE or '<空>'}；"
            f"允许值为 {', '.join(REVIEW_MODES)}"
        )
    try:
        validate_user_tokens()
    except TokenConfigError as exc:
        raise RuntimeError(f"访问令牌配置错误：{exc}") from exc
    if is_loopback_host(HOST):
        return
    if not ALLOW_REMOTE:
        raise RuntimeError(
            "检测到 YUNCE_HOST 监听非回环地址，但未设置 YUNCE_ALLOW_REMOTE=1。"
            "如确需局域网访问，请显式开启远程访问并设置 YUNCE_ACCESS_TOKEN。"
        )
    if not ACCESS_TOKEN and not TOKENS_FILE:
        if not PERSONAL_AUTH:
            raise RuntimeError(
                "远程访问已开启，但未设置 YUNCE_ACCESS_TOKEN、YUNCE_TOKENS_FILE "
                "或 YUNCE_PERSONAL_AUTH。"
                "零鉴权的局域网工作台会暴露账号 Cookie 和私信，已拒绝启动。"
            )
        import user_auth

        if user_auth.auth_status().get("activeAdmins", 0) < 1:
            raise RuntimeError(
                "远程访问仅启用了实名登录，但数据库中还没有有效管理员。"
                "请先配置 YUNCE_ACCESS_TOKEN 完成首次引导，再移除应急令牌。"
            )
