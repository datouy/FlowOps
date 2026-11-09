# -*- coding: utf-8 -*-
"""凭据加密与脱敏。

**为什么必须加密**：``cookiesFile/<name>.json`` 存的是 Playwright
``storage_state`` 明文（cookie + localStorage）。一台机器上的任何一个
低权限进程、任何一次误提交的备份、任何一个同步盘，都等于把这台机器上
**所有平台账号的完全控制权**交出去——cookie 拿到就能直接调平台接口，
不需要再登录。

**为什么不照抄蚁小二**：它用 AES-ECB 且密钥硬编码在客户端，本项目的逆向
分析里直接读出了密钥。ECB 没有 IV、同一明文块产生同一密文块，可重放、
可模式分析；客户端内置密钥意味着拿到二进制就等于拿到密钥。这两点都不
是"强度不够"，是"设计错了"。

**这里的做法**：

* 算法：**AES-256-GCM**（带认证标签，能检测密文被篡改）。
* nonce：每次加密**随机生成 12 字节**，同一份凭据两次加密的密文不同。
* AAD：绑定 ``account_id``（或 cookie 文件名）。把 A 账号的密文改个文件名
  冒充 B 账号，GCM 认证会直接失败——密文与账号绑定，不能跨账号搬运。
* 主密钥：
  - 优先读环境变量 ``FLOWOPS_SECRET_KEY``（或 ``YUNCE_SECRET_KEY``）；
  - 其次用**系统凭据库**：Windows DPAPI（当前用户作用域）、macOS
    Keychain、Linux ``keyring``；
  - 都没有时退回到一个本机派生密钥文件（``db/.master_key``，权限 0600），
    并在日志里提示"这是降级方案"。
* 落地格式：``secrets/<name>.enc``，二进制 = ``magic(4) | version(1) |
  nonce(12) | ciphertext+tag``。

**兼容**：读的时候先看文件是不是本格式的密文；不是（老明文 JSON）就按
老逻辑读原文。写的时候一律写密文。这样可以逐个账号平滑迁移，不用停机。

**脱敏**：``mask_secret`` / ``mask_cookie_header`` 供日志与 API 响应使用，
保证任何一条日志里都不会出现完整的 cookie 值。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets as _secrets
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from loguru import logger

from conf import BASE_DIR


# --------------------------------------------------------------------------
# 常量

MAGIC = b"FLC1"          # FlowOps Credential v1
VERSION = 1
NONCE_SIZE = 12
KEY_SIZE = 32            # AES-256
TAG_SIZE = 16            # GCM 认证标签

# 密文文件后缀。加密后的凭据不再使用 ``.json``，避免工具误当 JSON 解析。
ENC_SUFFIX = ".enc"
# 明文（迁移前）使用的后缀。
LEGACY_SUFFIX = ".json"

SECRET_DIR_NAME = "secrets"
MASTER_KEY_ENV = "FLOWOPS_SECRET_KEY"
MASTER_KEY_ALT_ENV = "YUNCE_SECRET_KEY"
MASTER_KEY_FILE = Path(BASE_DIR) / "db" / ".master_key"

# 需要脱敏的 cookie 名（小写匹配）。这些值一旦泄露等于账号失守。
SENSITIVE_COOKIE_NAMES = frozenset({
    "sessionid", "sessionid_ss", "sessionid_sr", "sessdata", "sid", "uid_tt",
    "uid_tt_ss", "passport_csrf_token", "passport_auth_status", "ttwid",
    "msToken", "odin_tt", "webid", "xs", "xt", "wxuin", "wxpluginkey",
    "devicetype", "bduss", "stoken", "ld", "mswechat",
})


# --------------------------------------------------------------------------
# 主密钥

def _derive_key_from_password(password: str, salt: bytes) -> bytes:
    """用 PBKDF2 从口令派生主密钥。"""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 480_000, dklen=KEY_SIZE,
    )


def _load_master_key_file() -> bytes | None:
    """读取本机降级密钥文件。"""
    try:
        raw = MASTER_KEY_FILE.read_bytes()
    except OSError:
        return None
    raw = raw.strip()
    if len(raw) < 32:
        return None
    try:
        return base64.urlsafe_b64decode(raw)
    except Exception:
        return None


def _store_master_key_file(key: bytes) -> None:
    """写入本机降级密钥文件（0600）。"""
    try:
        MASTER_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        MASTER_KEY_FILE.write_bytes(base64.urlsafe_b64encode(key))
        if os.name != "nt":
            os.chmod(MASTER_KEY_FILE, 0o600)
    except OSError as exc:
        logger.warning("凭据主密钥落盘失败（后续将无法解密）: {}", exc)


def _windows_dpapi_key() -> bytes | None:
    """Windows DPAPI：用当前用户凭据保护一个随机主密钥。

    用 USER_SCOPE：只有当前 Windows 账号能取回，其它用户/其它机器拿不到。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
    except Exception:
        return None
    try:
        CRYPTPROTECT_UI_FORBIDDEN = 0x1
        payload = _dpapi_payload_path()
        if not payload.is_file():
            return None
        raw = payload.read_bytes()
        out_buffer = ctypes.create_string_buffer(len(raw) + 64)
        out_blob = _new_data_blob()
        out_blob.cbData = len(raw) + 64
        out_blob.pbData = ctypes.cast(
            out_buffer, ctypes.POINTER(ctypes.c_char),
        )
        desc = ctypes.c_wchar_p("FlowOps master key")
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(_blob_from_bytes(raw)),
            ctypes.byref(desc),
            None, None, None, CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        ):
            return None
        value = bytes(out_blob.pbData[: out_blob.cbData])
        return value if len(value) == KEY_SIZE else None
    except Exception as exc:
        logger.debug("DPAPI 解密主密钥失败: {}", exc)
        return None


def _dpapi_payload_path() -> Path:
    return Path(BASE_DIR) / "db" / ".master_key.dpapi"


def _keyring_key() -> bytes | None:
    """Linux/macOS 系统凭据库。"""
    try:
        import keyring  # type: ignore
    except Exception:
        return None
    try:
        raw = keyring.get_password("FlowOps", "master-key")
        if not raw:
            return None
        value = base64.urlsafe_b64decode(raw)
        return value if len(value) == KEY_SIZE else None
    except Exception as exc:
        logger.debug("系统凭据库读取主密钥失败: {}", exc)
        return None


def _create_dpapi_protected_key(key: bytes) -> None:
    """把主密钥用 DPAPI 保护后落盘（Windows）。

    DPAPI 的正确用法：输出也必须用 ``DATA_BLOB`` 接收，由它告诉我们实际
    长度。直接把输出当成普通 buffer 再去切片会拿到 ``bytes`` 而非整数
    长度。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except Exception:
        return
    try:
        CRYPTPROTECT_UI_FORBIDDEN = 0x1
        out_buffer = ctypes.create_string_buffer(1024)
        out_blob = _new_data_blob()
        out_blob.cbData = 1024
        out_blob.pbData = ctypes.cast(
            out_buffer, ctypes.POINTER(ctypes.c_char),
        )
        desc = ctypes.c_wchar_p("FlowOps master key")
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(_blob_from_bytes(key)),
            ctypes.byref(desc),
            None, None, None, CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        ):
            return
        protected = bytes(out_blob.pbData[: out_blob.cbData])
        path = _dpapi_payload_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(protected)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except Exception as exc:
        logger.debug("DPAPI 保护主密钥失败: {}", exc)


class _DATA_BLOB(ctypes.Structure):  # noqa: N801 - 贴近 Win32 原生命名
    """Win32 ``DATA_BLOB`` 结构。

    既用于输入（``pbData`` 指向我们要加密的数据），也用于输出（由
    CryptProtectData 填入结果与真实长度）。
    """

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _new_data_blob():
    """新建一个空的 DATA_BLOB（用于接收 Win32 API 的输出）。"""
    return _DATA_BLOB()


def _blob_from_bytes(data: bytes):
    """把字节串包成 Win32 ``DATA_BLOB``（输入用）。

    ``pbData`` 必须指向一块**持续存活**的内存——这里把 buffer 挂在 blob 上，
    防止被 GC 回收后指向野内存。
    """
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
    # 保住 buffer 的引用。
    blob._keepalive = buffer  # type: ignore[attr-defined]
    return blob


_master_key_cache: bytes | None = None


def master_key() -> bytes:
    """取主密钥；必要时生成并持久化。

    优先级：环境变量 → 系统凭据库 → 本机降级文件。
    """
    global _master_key_cache
    if _master_key_cache is not None:
        return _master_key_cache

    for name in (MASTER_KEY_ENV, MASTER_KEY_ALT_ENV):
        raw = os.environ.get(name)
        if raw and raw.strip():
            # 环境变量给的是口令，不是裸密钥：加盐派生，避免"贴错 32 字符
            # 就当密钥用"这类事故。盐固定，保证重启后能解出同一批密文。
            key = _derive_key_from_password(raw.strip(), b"flowops-secret-v1")
            _master_key_cache = key
            return key

    key = _keyring_key()
    if key is None:
        key = _windows_dpapi_key()
    if key is None:
        key = _load_master_key_file()
    if key is None:
        key = _secrets.token_bytes(KEY_SIZE)
        stored = _keyring_key()
        if stored is None and sys.platform == "win32":
            _create_dpapi_protected_key(key)
            if _windows_dpapi_key() is None:
                # DPAPI 不可用（少见），退回明文文件 + 权限收紧。
                _store_master_key_file(key)
        elif stored is None:
            _store_master_key_file(key)
        logger.warning(
            "未配置 {} 环境变量，已自动生成凭据主密钥。"
            "生产环境请设置该环境变量，否则换机器/重装后无法解密已保存的凭据。",
            MASTER_KEY_ENV,
        )
    _master_key_cache = key
    return key


def reset_master_key_cache() -> None:
    """仅测试用：清掉进程内的密钥缓存。"""
    global _master_key_cache
    _master_key_cache = None


# --------------------------------------------------------------------------
# 路径

def secret_dir(base_dir: str | Path | None = None) -> Path:
    root = Path(base_dir) if base_dir else Path(BASE_DIR)
    return root / SECRET_DIR_NAME


def secret_path(cookie_path: str | Path, base_dir: str | Path | None = None) -> Path:
    """明文 cookie 文件 → 对应的密文文件路径。

    密文与原文件**同目录、不同后缀**（``.json`` → ``.enc``）。同目录有两个
    好处：

    * 账号凭据的增删改都在一个目录里，不会出现"删了账号密文还在"；
    * 目录整体迁移/备份时两者不会走散。

    （早期版本把密文集中到全局 ``secrets/``，但那样一来同名的临时目录
    会互相串号——测试里用 tmp_path 造凭据会读到真实目录的密文。凭据是
    按账号目录组织的，不该跨目录解析。）
    """
    path = Path(cookie_path)
    name = path.name
    if name.endswith(ENC_SUFFIX):
        return path
    if name.endswith(LEGACY_SUFFIX):
        name = name[: -len(LEGACY_SUFFIX)] + ENC_SUFFIX
    return path.parent / name


def is_encrypted_file(path: str | Path) -> bool:
    """判断一个文件是不是本模块写的密文。"""
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == MAGIC
    except OSError:
        return False


def resolve_credential_path(path: str | Path) -> Path | None:
    """把"逻辑凭据路径"解析成磁盘上实际存在的文件。

    账号表里记的是逻辑名（``<32hex>.json``）。迁移后磁盘上可能是明文
    ``<32hex>.json``，也可能是密文 ``<32hex>.enc``。

    返回实际存在的那个路径；两个都不存在时返回 ``None``。

    **密文优先**：两个都存在时选密文——密文是迁移后的正式存储，明文可能
    是迁移前遗留的旧副本（可能更旧）。

    凡是判断"凭据在不在"的地方都必须走这个函数——直接
    ``Path("x.json").is_file()`` 在迁移后会把正常账号误判成"凭据丢失"。
    """
    target = Path(path)
    encrypted = target.with_name(
        target.name[: -len(LEGACY_SUFFIX)] + ENC_SUFFIX
        if target.name.endswith(LEGACY_SUFFIX)
        else target.name + ENC_SUFFIX
    )
    if encrypted.is_file():
        return encrypted
    if target.is_file():
        return target
    return None


def credential_exists(path: str | Path) -> bool:
    """凭据是否存在（明文或密文都算）。"""
    return resolve_credential_path(path) is not None


# --------------------------------------------------------------------------
# 加解密

def encrypt_bytes(plaintext: bytes, aad: str = "") -> bytes:
    """AES-256-GCM 加密，返回 ``magic|version|nonce|ciphertext``。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = _secrets.token_bytes(NONCE_SIZE)
    encryptor = AESGCM(master_key())
    ciphertext = encryptor.encrypt(nonce, plaintext, aad.encode("utf-8") if aad else None)
    return MAGIC + bytes([VERSION]) + nonce + ciphertext


def decrypt_bytes(blob: bytes, aad: str = "") -> bytes:
    """解密；失败抛 ``ValueError``。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(blob) < len(MAGIC) + 1 + NONCE_SIZE + TAG_SIZE:
        raise ValueError("凭据密文长度不合法")
    if blob[: len(MAGIC)] != MAGIC:
        raise ValueError("凭据密文魔数不匹配")
    version = blob[len(MAGIC)]
    if version != VERSION:
        raise ValueError(f"凭据密文版本不支持：{version}")
    nonce = blob[len(MAGIC) + 1: len(MAGIC) + 1 + NONCE_SIZE]
    ciphertext = blob[len(MAGIC) + 1 + NONCE_SIZE:]
    decryptor = AESGCM(master_key())
    try:
        return decryptor.decrypt(
            nonce, ciphertext, aad.encode("utf-8") if aad else None,
        )
    except Exception as exc:
        raise ValueError("凭据解密失败：主密钥不匹配或密文已被篡改") from exc


# --------------------------------------------------------------------------
# 存储态读写（对外主入口）

def _aad_for(cookie_path: str | Path) -> str:
    """AAD 绑定账号标识：密文不能跨账号搬运。

    这里必须用**逻辑名**（``<name>.json``）而不是磁盘上的实际文件名：
    迁移后磁盘上是 ``<name>.enc``，而加密时算的是 ``<name>.json``。两边
    必须归一到同一个字符串，否则读回时会因为 AAD 不匹配而解密失败。
    """
    name = Path(cookie_path).name
    if name.endswith(ENC_SUFFIX):
        name = name[: -len(ENC_SUFFIX)] + LEGACY_SUFFIX
    return name


def write_storage_state(
    cookie_path: str | Path,
    state: dict[str, Any],
    base_dir: str | Path | None = None,
) -> Path:
    """把 storage_state 加密落盘，返回密文文件路径。

    调用方原本用 ``context.storage_state(path=...)`` 直接写明文，这里改成
    "先拿到明文 dict → 加密 → 写 ``secrets/*.enc``"。老明文文件在迁移
    成功后会被删除。
    """
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    blob = encrypt_bytes(payload.encode("utf-8"), _aad_for(cookie_path))
    target = secret_path(cookie_path, base_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def read_storage_state(
    cookie_path: str | Path,
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """读 storage_state，优先读密文，其次回落老明文。

    返回 ``None`` 表示"没有可用凭据"。解密失败**不静默返回 None**——
    那会让账号看起来像"没登录"，运维会误以为是扫码问题，实际是密钥丢了。
    这里向上抛，调用方应该显式处理。
    """
    enc = secret_path(cookie_path, base_dir)
    if enc.is_file():
        blob = enc.read_bytes()
        payload = decrypt_bytes(blob, _aad_for(cookie_path)).decode("utf-8")
        data = json.loads(payload)
        if isinstance(data, list):
            return {"cookies": data, "origins": []}
        if isinstance(data, dict):
            return {
                "cookies": data.get("cookies") if isinstance(data.get("cookies"), list) else [],
                "origins": data.get("origins") if isinstance(data.get("origins"), list) else [],
            }
        return None

    legacy = Path(cookie_path)
    if legacy.is_file():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if isinstance(data, list):
            return {"cookies": data, "origins": []}
        if isinstance(data, dict):
            return {
                "cookies": data.get("cookies") if isinstance(data.get("cookies"), list) else [],
                "origins": data.get("origins") if isinstance(data.get("origins"), list) else [],
            }
    return None


def materialize_storage_state(
    cookie_path: str | Path,
    base_dir: str | Path | None = None,
) -> str | None:
    """把密文凭据解成**临时明文文件**，给必须用文件路径的调用方。

    Playwright 的 ``new_context(storage_state=...)`` 只接受文件路径，没法直接
    喂 dict。少数旧适配器（alipay / tk / youtube / hupu）还在用这个写法。

    这里落一个临时明文文件并登记清理：文件权限收紧到 0600，进程退出时
    删除。它是"用完即焚"的临时副本，不是长期存储——长期存储仍然只有密文。
    """
    import atexit
    import tempfile

    state = read_storage_state(cookie_path, base_dir)
    if not isinstance(state, dict):
        return None
    fd, temp_name = tempfile.mkstemp(prefix="flowops-cred-", suffix=".json")
    try:
        os.close(fd)
        Path(temp_name).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(temp_name, 0o600)
    except OSError:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        return None

    def _cleanup() -> None:
        try:
            os.unlink(temp_name)
        except OSError:
            pass

    atexit.register(_cleanup)
    return temp_name


def migrate_legacy_cookies(
    cookies_dir: str | Path | None = None,
    base_dir: str | Path | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """把老的明文 cookie 文件批量迁移成密文。

    迁移成功后**删除原明文文件**——留在盘上就等于没加密。删除失败会在
    返回值里报告，不静默忽略。
    """
    root = Path(cookies_dir) if cookies_dir else Path(BASE_DIR) / "cookiesFile"
    migrated: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    removed: list[str] = []

    if not root.is_dir():
        return {
            "migrated": [], "skipped": [], "failed": [], "removed": [],
            "dry_run": dry_run, "total": 0,
        }

    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix != LEGACY_SUFFIX:
            continue
        # 只迁移看起来像 storage_state 的文件。
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            skipped.append(path.name)
            continue
        if not isinstance(data, (dict, list)):
            skipped.append(path.name)
            continue
        if not isinstance(data, dict) and not isinstance(data, list):
            skipped.append(path.name)
            continue
        # 只有含 cookies / origins 的才当作凭据；其它 JSON（如指纹）跳过。
        if isinstance(data, dict) and not (
            data.get("cookies") is not None or data.get("origins") is not None
        ):
            skipped.append(path.name)
            continue

        state = (
            {"cookies": data, "origins": []}
            if isinstance(data, list)
            else {
                "cookies": data.get("cookies") or [],
                "origins": data.get("origins") or [],
            }
        )
        try:
            if dry_run:
                # 试运行只做"能不能迁"的判断，不落任何文件。
                # 真正写盘之前先验证主密钥可用，否则试运行通过了、
                # 真正执行时才发现密钥有问题就晚了。
                master_key()
                migrated.append(path.name)
                continue
            write_storage_state(path, state, base_dir)
            migrated.append(path.name)
        except Exception as exc:
            failed.append({"file": path.name, "error": str(exc)[:200]})
            continue
        if not dry_run:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:
                failed.append({"file": path.name, "error": f"删除原明文失败：{exc}"})

    return {
        "migrated": migrated,
        "skipped": skipped,
        "failed": failed,
        "removed": removed,
        "dry_run": dry_run,
        "total": len(migrated),
    }


# --------------------------------------------------------------------------
# 脱敏

def mask_secret(value: Any, keep: int = 4) -> str:
    """通用脱敏：只保留头尾少量字符。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}…{text[-keep:]}({len(text)})"


def mask_cookie_header(value: Any, keep: int = 4) -> str:
    """脱敏一段 cookie 串（``a=1; b=2``）。

    对**已知敏感名**（sessionid / SESSDATA / BDUSS…）完全隐藏值；其余只留
    头尾。未知名也照常脱敏——宁可多遮一点。
    """
    text = str(value or "")
    if not text:
        return ""
    parts: list[str] = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, _, val = chunk.partition("=")
            name = name.strip()
            if name.lower() in SENSITIVE_COOKIE_NAMES:
                parts.append(f"{name}=***")
            else:
                parts.append(f"{name}={mask_secret(val, keep)}")
        else:
            parts.append(mask_secret(chunk, keep))
    return "; ".join(parts)


def mask_storage_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """脱敏整个 storage_state，用于日志与 API 响应。"""
    if not isinstance(state, dict):
        return {}
    cookies = []
    for cookie in state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        item = {k: v for k, v in cookie.items() if k != "value"}
        if name.lower() in SENSITIVE_COOKIE_NAMES:
            item["value"] = "***"
        else:
            item["value"] = mask_secret(cookie.get("value"))
        cookies.append(item)
    origins = []
    for origin in state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        entries = []
        for item in origin.get("localStorage") or []:
            if not isinstance(item, dict):
                continue
            entries.append({
                "name": item.get("name"),
                "value": mask_secret(item.get("value")),
            })
        origins.append({"origin": origin.get("origin"), "localStorage": entries})
    return {"cookies": cookies, "origins": origins}


def credential_health() -> dict[str, Any]:
    """凭据健康快照：给就绪探针与运维页面用。"""
    cookies_root = Path(BASE_DIR) / "cookiesFile"
    encrypted = 0
    legacy = 0
    if cookies_root.is_dir():
        for path in cookies_root.iterdir():
            if not path.is_file():
                continue
            if path.suffix == ENC_SUFFIX:
                encrypted += 1
            elif path.suffix == LEGACY_SUFFIX and not path.name.startswith("."):
                legacy += 1
    key_ok = True
    key_error = ""
    try:
        master_key()
    except Exception as exc:
        key_ok = False
        key_error = str(exc)[:200]
    return {
        "encrypted_count": encrypted,
        "legacy_plaintext_count": legacy,
        "master_key_ok": key_ok,
        "master_key_error": key_error,
        "master_key_source": (
            "env" if any(os.environ.get(n) for n in (MASTER_KEY_ENV, MASTER_KEY_ALT_ENV))
            else "system-store" if (_keyring_key() or _windows_dpapi_key())
            else "local-file"
        ),
        "credential_dir": str(cookies_root),
    }
