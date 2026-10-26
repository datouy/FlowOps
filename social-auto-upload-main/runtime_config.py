# -*- coding: utf-8 -*-
"""服务运行参数：监听地址、跨域来源与可选访问令牌。

默认只监听回环地址，避免同局域网的其他设备在无鉴权的情况下
打开工作台、使用本机已登录账号发内容或收发私信。
需要通过手机/另一台电脑访问时，显式设置 YUNCE_HOST=0.0.0.0，
并建议同时设置 YUNCE_ACCESS_TOKEN。
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5409
DEFAULT_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
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


def token_ok(candidate: str) -> bool:
    if not ACCESS_TOKEN:
        return True
    return hmac.compare_digest(str(candidate or ""), ACCESS_TOKEN)


def access_allowed(method: str, path: str, headers, args) -> bool:
    """判断一次请求是否可以通过可选的访问令牌校验。"""
    if not ACCESS_TOKEN:
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
    if not ACCESS_TOKEN:
        raise RuntimeError(
            "远程访问已开启，但未设置 YUNCE_ACCESS_TOKEN。"
            "零鉴权的局域网工作台会暴露账号 Cookie 和私信，已拒绝启动。"
        )
