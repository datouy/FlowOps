# -*- coding: utf-8 -*-
"""发布前预检：把"注定失败"的子任务挡在浏览器动作之前。

**为什么需要它**：现在的失败大多不是"平台拒绝了"，而是**本可以提前发现**的：

* 素材文件被删了 / 传到一半是 0 字节 → 起浏览器、登录态校验、进上传页、
  等到点击上传才 `FileNotFound`，白烧一次账号动作和两分钟；
* 账号 Cookie 文件被手工清掉、内容不是合法 JSON → 同样走到深处才炸；
* 代理配置写错（比如填了 `proxy.example.com:8080` 少了 scheme）→ Playwright
  起不来，或者更糟：静默走本机 IP，把真实出口和账号永久绑定；
* 指纹 sidecar 丢了 → 每次发布都像换了一台新设备；
* 文案含绝对化用语/手机号 → 平台侧审核不通过，但我们记成"发布失败"。

这些都不是"平台不稳定"，而是**输入不合法**。预检把它们一次性归到
``precheck_failed``，不消耗重试次数、不占用账号动作、不影响账号健康状态。

**与 `compliance.py` 的分工**：合规检查面向**编辑期**（前端即时提示），
预检面向**执行期**（后端门禁）。两者共用同一份规则表，但预检额外覆盖
素材/账号/代理/指纹/磁盘这些"编辑器看不到的运行环境"。

**原则**：预检只做**只读**判断，绝不修改状态；任何单项检查抛异常都降级为
警告，不能让预检本身成为新的故障点。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from loguru import logger


BLOCKING = "blocking"
WARNING = "warning"

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".flv", ".webm")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
PROXY_SCHEMES = ("http://", "https://", "socks5://")

# 发布前要求的最低磁盘余量（视频临时文件、封面生成都要落盘）
DISK_HEADROOM = 200 * 1024 * 1024


def _base_dir() -> Path:
    try:
        from conf import BASE_DIR
        return Path(BASE_DIR)
    except Exception:
        return Path(__file__).resolve().parent


def preflight_enabled() -> bool:
    """发布前预检的开关（默认开）。

    单测是**隔离文件系统**的：它们造的是"假账号名 + 不存在的素材文件"，
    预检一定会拦下来。所以 conftest 里默认关掉；真实运行默认开——预检依赖
    真实的 ``cookiesFile`` / ``videoFile`` / 指纹 sidecar，缺一不可。
    """
    raw = str(os.environ.get("FLOWOPS_PUBLISH_PREFLIGHT", "1")).strip().lower()
    return raw not in ("0", "off", "false", "no")


def _issue(code: str, level: str, message: str, hint: str = "") -> dict[str, Any]:
    return {"code": code, "level": level, "message": message, "hint": hint}


def cookie_file(cookie: str, base: Path | None = None) -> Path:
    """账号凭据文件的实际路径（与 uploader 侧的拼法保持一致）。"""
    base = base or _base_dir()
    return base / "cookiesFile" / str(cookie or "")


# ---------------------------------------------------------------- 单项检查

def check_assets(files: list[str], base: Path | None = None, content_kind: str = "video") -> list[dict]:
    base = base or _base_dir()
    issues: list[dict] = []
    if not files:
        issues.append(_issue(
            "asset_none", BLOCKING, "没有可发布的素材",
            "素材被删掉了，或者任务里本来就没带文件",
        ))
        return issues
    allowed = IMAGE_EXT + VIDEO_EXT
    root = base / "videoFile"
    for name in files:
        name = str(name or "").strip()
        if not name:
            issues.append(_issue("asset_empty_name", BLOCKING, "任务里有空文件名的素材"))
            continue
        path = root / name
        try:
            if not path.is_file():
                issues.append(_issue(
                    "asset_missing", BLOCKING, f"素材文件不存在：{name}",
                    "文件可能被清理了；请在素材管理里重新上传并重建任务",
                ))
                continue
            size = path.stat().st_size
            if size <= 0:
                issues.append(_issue(
                    "asset_zero_bytes", BLOCKING, f"素材是空文件：{name}",
                    "上传中断导致；重新上传该素材",
                ))
                continue
            if path.suffix.lower() not in allowed:
                issues.append(_issue(
                    "asset_type", WARNING,
                    f"素材扩展名不在白名单：{name}",
                    "平台可能拒绝该格式，建议转成 mp4/jpg/png",
                ))
        except Exception as exc:
            issues.append(_issue("asset_error", WARNING, f"检查素材失败：{name}（{exc}）"[:200]))
    if content_kind == "video" and len(files) > 1:
        issues.append(_issue(
            "asset_multi_video", WARNING,
            f"视频任务带了 {len(files)} 个素材，多数平台只接受 1 个视频",
        ))
    return issues


def check_account(cookie: str, base: Path | None = None) -> list[dict]:
    """凭据是否还在、还能读出来。

    凭据可能是明文 ``x.json``，也可能是迁移后的密文 ``x.enc``——判断"在不在"
    必须走 ``credential_exists``/``read_storage_state``，直接 ``is_file()``
    会把加密账号全判成"凭据丢失"（这是这套代码里最容易踩的一个坑）。
    """
    issues: list[dict] = []
    if not cookie:
        issues.append(_issue(
            "account_none", BLOCKING, "子任务没有绑定账号凭据",
        ))
        return issues
    logical = cookie_file(cookie, base)
    try:
        from utils.credential_crypto import credential_exists, read_storage_state
    except Exception as exc:
        return [_issue("account_check_error", WARNING, f"凭据模块不可用：{exc}"[:200])]
    try:
        if not credential_exists(logical):
            issues.append(_issue(
                "account_cookie_missing", BLOCKING, f"账号凭据文件不存在：{cookie}",
                "该账号可能已被删除；到账号管理重新扫码登录",
            ))
            return issues
        try:
            state = read_storage_state(logical)
        except Exception as exc:
            # 解密失败**不能**当成"没登录"：那是主密钥丢了/换了机器，
            # 重新扫码会把问题掩盖成"偶发掉线"。
            issues.append(_issue(
                "account_cookie_undecryptable", BLOCKING,
                f"账号凭据解密失败：{cookie}",
                f"主密钥可能已丢失或被更换（{exc}）；需要恢复密钥或重新扫码登录",
            ))
            return issues
        if not state:
            issues.append(_issue(
                "account_cookie_unreadable", BLOCKING, f"账号凭据读不出内容：{cookie}",
                "凭据文件被截断或手工改坏了；重新扫码登录",
            ))
            return issues
        if not (state.get("cookies") or state.get("origins")):
            issues.append(_issue(
                "account_cookie_no_cookies", BLOCKING, f"账号凭据里没有 Cookie：{cookie}",
                "登录态没保存成功；重新扫码登录",
            ))
    except Exception as exc:
        issues.append(_issue(
            "account_cookie_error", WARNING, f"检查账号凭据失败：{cookie}（{exc}）"[:200],
        ))
    return issues


def _resolved_cookie_path(cookie: str, base: Path | None = None) -> Path:
    """账号运行时实际使用的凭据路径（密文优先）。

    指纹和环境 sidecar 的命名规则跟着这个路径走（``x.enc`` 与 ``x.json``
    算出来的是两个不同文件），所以必须和 ``platform_reply`` /
    ``account_browser`` 用同一个解析结果，否则会"看着有指纹、其实选了另一份"。
    """
    logical = cookie_file(cookie, base)
    try:
        from utils.credential_crypto import resolve_credential_path
        return resolve_credential_path(logical) or logical
    except Exception:
        return logical


def check_proxy(cookie: str, base: Path | None = None) -> list[dict]:
    """代理配置要么没有，要么必须是能用的形态。

    配置了代理但地址非法，是最危险的一种"小错"：看起来有隔离，实际请求
    要么直接失败，要么在部分实现里回落直连——一次直连就把真实 IP 和账号
    永久绑在一起。
    """
    issues: list[dict] = []
    try:
        from utils.account_environment import load_environment
        env = load_environment(_resolved_cookie_path(cookie, base))
    except Exception as exc:
        return [_issue("proxy_error", WARNING, f"读取账号环境失败：{exc}"[:200])]
    proxy = (env or {}).get("proxy") or {}
    server = str(proxy.get("server") or "").strip()
    if not server:
        from runtime_config import REQUIRE_PROXY
        severity = BLOCKING if REQUIRE_PROXY else WARNING
        issues.append(_issue(
            "proxy_absent", severity,
            "该账号没有绑定独立出口，发布将使用本机 IP",
            "多账号共用本机出口容易被判定为账号矩阵；请在代理池里分配独立出口",
        ))
        return issues
    if not server.lower().startswith(PROXY_SCHEMES):
        issues.append(_issue(
            "proxy_scheme", BLOCKING, f"代理地址缺少协议头：{server}",
            "必须是 http://、https:// 或 socks5:// 开头",
        ))
    if "@" in server:
        issues.append(_issue(
            "proxy_inline_auth", WARNING,
            "代理地址里带了凭据，建议改用账号密码字段",
        ))
    return issues


def check_fingerprint(cookie: str, base: Path | None = None) -> list[dict]:
    """指纹 sidecar 是否还在，以及指纹本身是否自相矛盾。"""
    issues: list[dict] = []
    try:
        from utils.account_fingerprint import fingerprint_consistency, load_fingerprint
        fingerprint = load_fingerprint(_resolved_cookie_path(cookie, base))
    except Exception as exc:
        return [_issue("fingerprint_error", WARNING, f"读取指纹失败：{exc}"[:200])]
    if not fingerprint:
        issues.append(_issue(
            "fingerprint_missing", WARNING,
            "该账号没有指纹记录，首次发布将现场生成一套新设备身份",
            "老账号建议重新扫码登录一次，把当代设备身份固定下来",
        ))
        return issues
    try:
        problems = fingerprint_consistency(fingerprint)
    except Exception as exc:
        return [_issue("fingerprint_error", WARNING, f"指纹自检失败：{exc}"[:200])]
    for problem in problems[:3]:
        issues.append(_issue(
            "fingerprint_inconsistent", WARNING,
            f"指纹自相矛盾：{problem}",
            "平台侧一次高熵取值就能看出伪造；建议按当前内核重建该账号指纹",
        ))
    return issues


def check_compliance(job, extra: dict, platform_type: int) -> list[dict]:
    """复用编辑期同一套规则，只把"错误"升级为阻塞项。"""
    try:
        import compliance
        from publish_jobs import json_loads
        data = {
            "type": platform_type,
            "title": job["title"],
            "desc": extra.get("desc", ""),
            "tags": json_loads(job["tags"]) if job["tags"] else [],
            "fileList": json_loads(job["file_list"]) if job["file_list"] else [],
            "contentType": extra.get("contentType", "video"),
        }
        result = compliance.check_content(data)
    except Exception as exc:
        return [_issue("compliance_error", WARNING, f"合规检查失败：{exc}"[:200])]
    issues = [
        _issue("compliance", BLOCKING, str(message))
        for message in (result.get("errors") or [])
    ]
    issues.extend(
        _issue("compliance_warn", WARNING, str(message))
        for message in (result.get("warnings") or [])
    )
    return issues


def check_disk(base: Path | None = None) -> list[dict]:
    base = base or _base_dir()
    try:
        free = shutil.disk_usage(str(base)).free
    except Exception:
        return []
    if free < DISK_HEADROOM:
        return [_issue(
            "disk_low", BLOCKING,
            f"磁盘剩余 {free / 1024 / 1024:.0f}MB，低于发布所需余量",
            "清理素材或日志后重试；写满会导致下载/封面生成失败",
        )]
    return []


# ---------------------------------------------------------------- 汇总

def preflight_publish(job, item: dict, *, platform_type: int | None = None,
                      base: Path | None = None, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    """对一条发布子任务做完整预检。

    ``job`` 是 ``jobs`` 表的行（含 title/tags/file_list/extra），
    ``item`` 是 ``publish_items`` 的行（含 cookie/file）。返回::

        {"ok": bool, "blockers": [...], "warnings": [...], "errors": [code...]}

    ``skip`` 用于按名字跳过某些检查（``assets`` / ``account`` / ``proxy`` /
    ``fingerprint`` / ``compliance`` / ``disk``），测试与"只校验运行环境"的
    场景会用到。
    """
    base = base or _base_dir()
    try:
        from publish_jobs import json_loads
        extra = json_loads(job["extra"] or "{}") if job["extra"] else {}
    except Exception:
        extra = {}
    content_kind = str(extra.get("contentType") or "video")
    if platform_type is None:
        try:
            platform_type = int(job["type"])
        except Exception:
            platform_type = 0

    cookie = str(item.get("cookie") or "")
    if content_kind in ("image-text", "article"):
        try:
            from publish_jobs import json_loads
            files = json_loads(job["file_list"]) or []
        except Exception:
            files = []
        if not files:
            files = [str(item.get("file") or "")]
    else:
        files = [str(item.get("file") or "")]

    checks = (
        ("assets", lambda: check_assets(files, base, content_kind)),
        ("account", lambda: check_account(cookie, base)),
        ("proxy", lambda: check_proxy(cookie, base)),
        ("fingerprint", lambda: check_fingerprint(cookie, base)),
        ("compliance", lambda: check_compliance(job, extra, platform_type)),
        ("disk", lambda: check_disk(base)),
    )
    skipped = {str(name) for name in skip}
    issues: list[dict] = []
    for name, check in checks:
        if name in skipped:
            continue
        try:
            issues.extend(check())
        except Exception as exc:
            logger.exception("发布预检单项异常")
            issues.append(_issue("preflight_error", WARNING, f"预检异常：{exc}"[:200]))

    # 预检和合规检查有重叠（素材存在性、标题长度），按文案去重，
    # 免得同一条原因在 UI 上刷两遍。
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for issue in issues:
        key = (issue["level"], str(issue.get("message") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)

    blockers = [item for item in unique if item["level"] == BLOCKING]
    warnings = [item for item in unique if item["level"] == WARNING]
    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "errors": [item["code"] for item in blockers],
        "checkedAt": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
    }


def summarize(result: dict[str, Any], limit: int = 2) -> str:
    """把预检结果压成一行人话，用于写进 item.error。"""
    messages = [str(item.get("message") or "") for item in (result.get("blockers") or [])]
    if not messages:
        return "预检未通过"
    head = "；".join(messages[:limit])
    if len(messages) > limit:
        head += f"（另有 {len(messages) - limit} 项）"
    return head[:280]
