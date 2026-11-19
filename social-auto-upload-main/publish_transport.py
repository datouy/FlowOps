# -*- coding: utf-8 -*-
"""API 优先的发布通道：RPA 只负责传文件，平台创建接口负责提交。

现状：抖音、小红书、视频号的发布全靠页面自动化（填表单 + 点发布）。RPA 能
覆盖的功能多，但有三个硬伤：

1. 慢：每个作品都要开页面、选文件、等转码前置渲染、逐个填字段；
2. 脆：平台改版一次选择器就可能整体失效；
3. 贵：浏览器资源占用高，和私信同步、状态查询抢同一个账号 profile 锁。

发布链路采用"API 直发为主，RPA 补文件上传和复杂能力"的方式，
落地一个**混合通道**：

    文件上传 → 仍然走 RPA（平台的上传接口需要分片/预签名，实现成本高）
    创建作品 → 走平台自己的创建接口（从浏览器里学习到的请求模板）

实现方式是**录制-回放**：

* 第一次发布仍然走 RPA，但顺手录下浏览器发出的"创建作品"请求
  （URL、方法、请求头、请求体）和这次上传拿到的素材 ID；
* 把标题、正文、话题、素材 ID 这些每次都不同的字段替换成占位符，
  存成该账号的"发布模板"；
* 之后的发布：RPA 传完文件拿到新的素材 ID → 用模板渲染出请求体 →
  在页面上下文里直接 ``fetch`` 创建接口。

安全约束（避免重复发布，比"直发"本身更重要）：

* 模板不存在 → 走 RPA；
* 素材 ID 还没准备好 / 模板有未解析的占位符 → 走 RPA；
* 平台**明确拒绝**（HTTP 4xx 或业务错误码）→ 走 RPA；
* 请求结果**不确定**（超时、断网）→ **绝不**再点一次发布按钮，
  而是记成"已提交，未验证"，交给状态查询线程去平台核对。

目前支持抖音（3）、小红书（1）和视频号（2）。视频号的文件上传仍由 RPA 完成，
创建作品时回放页面录制到的 ``post_create`` 请求；只对普通即时视频启用，复杂
表单选项继续走 RPA。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from loguru import logger


# ---------------------------------------------------------------- 通道开关

TRANSPORT_ENV = "FLOWOPS_PUBLISH_TRANSPORT"
TRANSPORT_UI = "ui"
TRANSPORT_HYBRID = "hybrid"

# 各平台"创建作品"请求的 URL 特征。
CREATE_URL_MARKERS: dict[int, tuple[str, ...]] = {
    1: ("/web_api/sns/v2/note", "/api/sns/web/v1/note"),
    2: ("/cgi-bin/mmfinderassistant-bin/post/post_create",),
    3: ("/aweme/create_v2/", "/aweme/create/"),
}
# 同一前缀下的其它接口（列表、删除等）不能误判成创建请求。
CREATE_URL_EXCLUDES: dict[int, tuple[str, ...]] = {
    1: ("/note/manage", "/note/delete", "/note/post", "/note/list", "/note/comment"),
    2: (
        "/post/post_draft", "/post/post_clip_video", "/post/post_list",
        "/post/post_delete", "/post/post_search",
    ),
    3: ("/aweme/create_list/", "/aweme/delete/"),
}

# 需要重新签名的平台：小红书的 x-s / x-t 是一次性的，录下来不能直接用。
SIGNING_PLATFORMS = {1}

# 上传相关响应的 URL 特征：只从这些响应里收集素材 ID。
UPLOAD_URL_MARKERS = (
    "upload", "tus", "tos-", "vod", "/video/", "/image/", "preupload",
)

# 各平台认哪些素材 ID 字段（按优先级）。同一字段出现多个不同取值说明这次
# 上传还没结束或者混进了别的资源，回放时无法确定该用哪个，索性放弃直发。
MEDIA_ID_KEYS: dict[int, tuple[str, ...]] = {
    1: ("file_id", "fileId", "image_id", "imageId", "media_id", "material_id"),
    2: ("draftId", "clipKey", "DownloadURL"),
    3: ("vid", "video_id", "videoid", "upload_id", "material_id", "media_id"),
}

TENCENT_CREATE_HOST = "https://finder.video.qq.com"
TENCENT_VIDEO_FILETYPE = "20302"
TENCENT_COVER_FILETYPE = "20304"

# 视频号创建请求里的资源与任务字段。按路径替换，避免相同数字（宽、高、时间戳）
# 在请求体其它位置被误替换。报告接口里 width/height 的命名与媒体对象相反，
# 这是平台自身的行为，这里照录。
_TENCENT_PATH_KEYS: dict[str, tuple[tuple[str | int, ...], ...]] = {
    "video_url": (
        ("objectDesc", "media", 0, "url"),
        ("megavideoDesc", "media", 0, "url"),
    ),
    "cover_url": (
        ("objectDesc", "media", 0, "coverUrl"),
        ("objectDesc", "media", 0, "fullCoverUrl"),
        ("objectDesc", "media", 0, "fullThumbUrl"),
        ("objectDesc", "media", 0, "thumbUrl"),
        ("megavideoDesc", "media", 0, "coverUrl"),
        ("megavideoDesc", "media", 0, "thumbUrl"),
    ),
    "share_cover_url": (
        ("objectDesc", "media", 0, "shareCoverUrl"),
    ),
    "video_md5": (
        ("objectDesc", "media", 0, "md5sum"),
        ("megavideoDesc", "media", 0, "md5sum"),
    ),
    "file_size": (
        ("objectDesc", "media", 0, "fileSize"),
        ("report", "fileSize"),
        ("megavideoDesc", "media", 0, "fileSize"),
    ),
    "video_width": (
        ("objectDesc", "media", 0, "width"),
        ("report", "height"),
        ("megavideoDesc", "media", 0, "width"),
    ),
    "video_height": (
        ("objectDesc", "media", 0, "height"),
        ("report", "width"),
        ("megavideoDesc", "media", 0, "height"),
    ),
    "duration": (
        ("objectDesc", "media", 0, "videoPlayLen"),
        ("report", "duration"),
        ("megavideoDesc", "media", 0, "videoPlayLen"),
    ),
    "duration_ms": (
        ("megavideoDesc", "media", 0, "videoPlayLenMs"),
    ),
    "draft_id": (
        ("objectDesc", "media", 0, "urlCdnTaskId"),
        ("report", "draftId"),
        ("videoClipTaskId",),
    ),
    "clip_key": (
        ("report", "clipKey"),
    ),
    "upload_cost": (
        ("report", "uploadCost"),
    ),
}

# 请求头里不能回放的东西。
_DROP_HEADERS = {
    "content-length", "host", "cookie", "connection", "accept-encoding",
    "referer", "origin", "user-agent", "sec-fetch-dest", "sec-fetch-mode",
    "sec-fetch-site", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
}
# 小红书的签名头每次请求都不一样，回放时重新算。
_DROP_HEADERS_BY_PLATFORM: dict[int, tuple[str, ...]] = {
    1: ("x-s", "x-t", "x-s-common", "x-t-common"),
}


# ---------------------------------------------------------------- 通道模式

def transport_mode() -> str:
    """当前发布通道模式：``ui``（纯 RPA）或 ``hybrid``（API 优先）。"""
    mode = str(os.getenv(TRANSPORT_ENV, TRANSPORT_HYBRID) or "").strip().lower()
    return mode or TRANSPORT_HYBRID


def hybrid_enabled(platform_type: int) -> bool:
    """该平台是否可以使用混合通道（必须既有创建接口又开了开关）。"""
    try:
        platform = int(platform_type)
    except (TypeError, ValueError):
        return False
    if platform not in CREATE_URL_MARKERS:
        return False
    if transport_mode() != TRANSPORT_HYBRID:
        return False
    try:
        from myUtils.platform_registry import supports_api_publish

        if not supports_api_publish(platform):
            return False
    except Exception:
        return False
    return True


def transport_status(platform_type: int, cookie_path: str | Path = "") -> dict:
    """给前端看的通道状态：当前用哪条通道、有没有可复用的模板。"""
    enabled = hybrid_enabled(platform_type)
    template = None
    if enabled and cookie_path:
        template = load_template(cookie_path, platform_type)
    return {
        "platformType": int(platform_type),
        "mode": transport_mode(),
        "hybridEnabled": enabled,
        "templateReady": bool(template),
        "templateCreatedAt": str((template or {}).get("created_at") or ""),
        "description": (
            "API 直发（文件上传走 RPA，创建作品走平台接口）"
            if enabled else "页面自动化发布"
        ),
    }


def is_create_url(platform_type: int, url: str) -> bool:
    """判断一个 URL 是不是该平台的"创建作品"请求。"""
    text = str(url or "")
    if not any(marker in text for marker in CREATE_URL_MARKERS.get(int(platform_type), ())):
        return False
    return not any(
        marker in text
        for marker in CREATE_URL_EXCLUDES.get(int(platform_type), ())
    )


# ---------------------------------------------------------------- 模板存储

def template_dir(cookie_path: str | Path) -> Path:
    """模板目录跟着账号凭据走：``cookiesFile/publish_templates/``。

    放在凭据目录旁边而不是固定的全局目录，是为了让每个账号的模板天然隔离，
    也方便测试用临时目录覆盖。
    """
    return Path(cookie_path).expanduser().resolve().parent / "publish_templates"


def template_path(cookie_path: str | Path, platform_type: int) -> Path:
    stem = Path(cookie_path).stem or "account"
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in stem)
    return template_dir(cookie_path) / f"{safe}-p{int(platform_type)}.json"


def load_template(cookie_path: str | Path, platform_type: int) -> dict | None:
    """读取账号的发布模板；没有或损坏时返回 ``None``。"""
    path = template_path(cookie_path, platform_type)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not payload.get("url") or not isinstance(payload.get("body"), dict):
        return None
    return payload


def save_template(cookie_path: str | Path, platform_type: int, template: dict) -> bool:
    """保存发布模板；失败只记日志，不影响发布主流程。"""
    if not isinstance(template, dict) or not template.get("url"):
        return False
    path = template_path(cookie_path, platform_type)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return True
    except OSError as exc:
        logger.warning("发布模板保存失败（{}）: {}", path.name, exc)
        return False


def clear_template(cookie_path: str | Path, platform_type: int) -> bool:
    try:
        template_path(cookie_path, platform_type).unlink(missing_ok=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 素材 ID

def collect_media_ids(payloads: list[Any], platform_type: int = 0) -> dict[str, str]:
    """从上传响应里收集素材 ID。

    只保留"唯一一个取值"的字段：同一个字段出现多个不同取值说明这次上传
    还没结束或者混进了别的资源，回放时无法确定该用哪个，索性放弃直发。
    """
    keys = MEDIA_ID_KEYS.get(int(platform_type or 0)) or ()
    if not keys:
        return {}
    seen: dict[str, set[str]] = {}
    for payload in payloads or []:
        for item in _walk_dicts(payload):
            for key in keys:
                value = item.get(key)
                if not isinstance(value, (str, int)):
                    continue
                text = str(value).strip()
                if len(text) < 10:
                    continue
                seen.setdefault(key, set()).add(text)
    return {
        key: next(iter(values))
        for key, values in seen.items()
        if len(values) == 1
    }


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _parse_request_body(body_text: str) -> tuple[Any, str]:
    """解析录制到的请求体。

    支持 JSON 和表单编码两种常见格式；multipart（平台上传素材时常用）无法
    安全回放，返回 ``(None, "")`` 表示"不录模板"。
    """
    text = str(body_text or "")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            return json.loads(text), ""
        except ValueError:
            return None, ""
    if "multipart/form-data" in text[:200]:
        return None, ""
    if "=" in text and "\n" not in text.strip()[:200]:
        from urllib.parse import parse_qsl

        pairs = parse_qsl(text, keep_blank_values=True)
        if not pairs:
            return None, ""
        return dict(pairs), "application/x-www-form-urlencoded;charset=UTF-8"
    return None, ""


# ---------------------------------------------------------------- 文本占位符

def build_full_text(desc: str, tags: list[str]) -> str:
    """正文 + 话题的完整形态（与 RPA 输入顺序一致）。"""
    text = str(desc or "")
    for tag in tags or []:
        text += f" #{tag}"
    return text


def text_replacements(title: str, desc: str, tags: list[str]) -> list[tuple[str, str]]:
    """录制时用旧值替换成占位符的映射（长的先替换）。"""
    entries: list[tuple[str, str]] = []
    full_text = build_full_text(desc, tags)
    if full_text.strip():
        entries.append((full_text, "{{fulltext}}"))
    if str(desc or "").strip():
        entries.append((str(desc), "{{desc}}"))
    if str(title or "").strip():
        entries.append((str(title), "{{title}}"))
    for index, tag in enumerate(tags or []):
        marker = f"#{tag}"
        if marker.strip():
            entries.append((marker, "{{tag:%d}}" % index))
    entries.sort(key=lambda item: len(item[0]), reverse=True)
    return entries


def render_values(title: str, desc: str, tags: list[str]) -> dict[str, str]:
    """占位符 → 本次发布的真实取值。"""
    values = {
        "{{fulltext}}": build_full_text(desc, tags),
        "{{desc}}": str(desc or ""),
        "{{title}}": str(title or ""),
    }
    for index, tag in enumerate(tags or []):
        values["{{tag:%d}}" % index] = f"#{tag}"
    return values


def _apply_replacements(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, dict):
        return {key: _apply_replacements(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_apply_replacements(item, replacements) for item in value]
    if isinstance(value, str):
        text = value
        for old, new in replacements:
            if old and old in text:
                text = text.replace(old, new)
        return text
    return value


def _path_get(value: Any, path: tuple[str | int, ...]) -> Any:
    current = value
    for part in path:
        try:
            current = current[part]
        except (KeyError, IndexError, TypeError):
            return None
    return current


def _path_set(value: Any, path: tuple[str | int, ...], replacement: Any) -> bool:
    if not path:
        return False
    current = value
    for part in path[:-1]:
        try:
            current = current[part]
        except (KeyError, IndexError, TypeError):
            return False
    try:
        current[path[-1]] = replacement
    except (KeyError, IndexError, TypeError):
        return False
    return True


def _first_nested_value(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        lowered = {str(key).lower(): item for key, item in value.items()}
        for key in keys:
            item = lowered.get(key.lower())
            if item not in (None, ""):
                return item
        for item in value.values():
            found = _first_nested_value(item, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_nested_value(item, keys)
            if found not in (None, ""):
                return found
    return None


def normalize_tencent_download_url(value: Any) -> str:
    """把视频号上传接口返回的资源路径补成可回放的完整 URL。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("/"):
        return TENCENT_CREATE_HOST + text
    if text.startswith(("http://", "https://")):
        return text
    if "qq.com" in text:
        suffix = text.split("qq.com", 1)[1]
        return TENCENT_CREATE_HOST + (suffix if suffix.startswith("/") else "/" + suffix)
    return TENCENT_CREATE_HOST + "/" + text.lstrip("/")


def _tencent_filetype(headers: dict[str, Any]) -> str:
    arguments = ""
    for key, value in (headers or {}).items():
        if str(key).lower() == "x-arguments":
            arguments = str(value or "")
            break
    if not arguments:
        return ""
    parsed = parse_qs(arguments, keep_blank_values=True)
    return str((parsed.get("filetype") or [""])[0] or "")


def _tencent_is_full_post(width: Any, height: Any) -> int | None:
    try:
        width_value = float(width)
        height_value = float(height)
    except (TypeError, ValueError):
        return None
    if width_value <= 0 or height_value <= 0:
        return None
    return 1 if width_value / height_value <= 0.857 else 0


def _apply_tencent_placeholders(body: Any) -> Any:
    if not isinstance(body, dict):
        return body
    rendered = copy.deepcopy(body)
    for key, paths in _TENCENT_PATH_KEYS.items():
        marker = "{{media:%s}}" % key
        for path in paths:
            _path_set(rendered, path, marker)

    if isinstance(rendered.get("topics"), list):
        rendered["topics"] = "{{tencent:tags}}"
    object_desc = rendered.get("objectDesc")
    if isinstance(object_desc, dict) and isinstance(object_desc.get("shortTitle"), list):
        object_desc["shortTitle"] = "{{tencent:short_title_list}}"

    report = rendered.get("report")
    if isinstance(report, dict):
        report["timestamp"] = "{{tencent:timestamp}}"
        report["clientid"] = "{{tencent:client_id}}"
    if "timestamp" in rendered:
        rendered["timestamp"] = "{{tencent:timestamp}}"
    return rendered


def build_template(
    *,
    url: str,
    method: str,
    headers: dict[str, str],
    body: Any,
    title: str,
    desc: str,
    tags: list[str],
    media_ids: dict[str, str],
    signature: str = "",
) -> dict:
    """把一次真实发布请求变成可复用的模板。

    文本字段（标题/正文/话题）按字段值替换成占位符；素材 ID 按**值**匹配
    替换——平台上传接口的字段名和创建接口的字段名经常对不上（上传叫
    ``upload_id``、创建叫 ``vid``），按值匹配才不会漏。
    """
    platform_type = _platform_of_markers(url)
    kept_headers = {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() not in _DROP_HEADERS
        and str(key).lower() not in _DROP_HEADERS_BY_PLATFORM.get(platform_type, ())
    }
    text_repl = text_replacements(title, desc, tags)
    rendered = _apply_replacements(body, text_repl)
    media_keys = sorted((media_ids or {}).keys())
    if platform_type == 2:
        rendered = _apply_tencent_placeholders(rendered)
        # 视频号按路径生成占位符；不再按数值全局替换，避免 1080、时长、
        # 毫秒时间戳等短值误伤请求体里其它字段。
        media_keys = [
            key for key, paths in _TENCENT_PATH_KEYS.items()
            if any(_path_get(body, path) is not None for path in paths)
        ]
    # 文本替换后重新序列化，素材 ID 按值替换（值是长随机串，不会误伤）。
    text = json.dumps(rendered, ensure_ascii=False)
    used_media: dict[str, str] = {}
    for key in media_keys:
        placeholder = "{{media:%s}}" % key
        if placeholder in text:
            used_media[key] = placeholder
            continue
        if platform_type == 2:
            continue
        token = str((media_ids or {}).get(key) or "")
        if not token or token not in text:
            continue
        text = text.replace(token, placeholder)
        used_media[key] = placeholder
    try:
        final_body = json.loads(text)
    except ValueError:
        final_body = text
    template = {
        "url": str(url),
        "method": str(method or "POST").upper(),
        "headers": kept_headers,
        "body": final_body,
        "media_keys": sorted(used_media),
        "signature": str(signature or ""),
        "created_at": _now(),
    }
    if platform_type == 2 and isinstance(final_body, dict):
        width = (media_ids or {}).get("video_width")
        height = (media_ids or {}).get("video_height")
        try:
            duration = int((media_ids or {}).get("duration"))
        except (TypeError, ValueError):
            duration = 0
        full_post = _tencent_is_full_post(width, height)
        if full_post is None and final_body.get("isFullPost") in (0, 1):
            full_post = int(final_body["isFullPost"])
        template["requirements"] = {
            "isFullPost": full_post,
            "tagCount": len(tags or []),
            "longVideo": bool(
                duration > 60
                or "megavideoDesc" in final_body
                or final_body.get("mode") == 1
            ),
        }
    return template


def _platform_of_markers(url: str) -> int:
    for platform, markers in CREATE_URL_MARKERS.items():
        if any(marker in str(url) for marker in markers):
            return platform
    return 0


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 表单签名

def form_signature(
    *,
    product_link: str = "",
    collection: str = "",
    declaration: str = "",
    thumbnail: str = "",
    scheduled: bool = False,
    scheduled_at: Any = "",
) -> str:
    """当前发布用到的"表单选项"指纹。

    直发走的是录下来的请求模板，模板里带了上次发布时的表单选项（商品链接、
    合集、自主声明、封面、定时）。这次如果选项和上次不一致，就不能用旧模板
    直发——否则会把"这次选的东西"丢掉。所以直发前先比对指纹，不一致就回落 RPA。

    定时发布时间也进指纹：模板里存的是上次的发布时间，这次时间不同就必须
    回落 RPA，否则作品会按旧时间发出去。
    """
    import hashlib

    payload = "|".join((
        "product:" + (str(product_link or "").strip()[:120]),
        "collection:" + (str(collection or "").strip()[:120]),
        "declaration:" + (str(declaration or "").strip()[:60]),
        "thumbnail:" + ("1" if str(thumbnail or "").strip() else "0"),
        "scheduled:" + ("1" if scheduled else "0"),
        "scheduled_at:" + str(scheduled_at or "").strip()[:40],
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _render_value(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _render_value(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_value(item, values) for item in value]
    if not isinstance(value, str):
        return value
    if value in values:
        # 整个字段就是占位符时保留真实类型；视频号的宽高、文件大小和话题列表
        # 都不能被一律序列化成字符串。
        return copy.deepcopy(values[value])
    text = value
    for placeholder, replacement in values.items():
        if placeholder in text:
            text = text.replace(placeholder, str(replacement))
    return text


def render_body(template: dict, values: dict[str, Any]) -> Any:
    """把模板渲染成本次请求体。"""
    body = template.get("body")
    if isinstance(body, str):
        text = body
        for placeholder, value in values.items():
            if placeholder in text:
                text = text.replace(placeholder, str(value))
        try:
            return json.loads(text)
        except ValueError:
            return text
    return _render_value(body, values)


def template_placeholders(template: dict) -> set[str]:
    """模板里出现的所有占位符。"""
    text = json.dumps(template.get("body"), ensure_ascii=False)
    found: set[str] = set()
    index = 0
    while True:
        start = text.find("{{", index)
        if start < 0:
            break
        end = text.find("}}", start)
        if end < 0:
            break
        found.add(text[start:end + 2])
        index = end + 2
    return found


def required_media_keys(template: dict) -> set[str]:
    """模板里依赖素材 ID 的占位符对应的字段名。"""
    keys = set()
    for placeholder in template_placeholders(template):
        if placeholder.startswith("{{media:"):
            keys.add(placeholder[len("{{media:"):-2])
    return keys


# ---------------------------------------------------------------- 回放

# 小红书重签说明（对齐蚁小二 qianming 签名服务思路，但走本地页面重签）：
# x-s / x-t / x-s-common / x-t-common 都是一次性的，录制时直接丢弃，
# 回放时在页面上下文里调 `window._webmsxyw(url, bodyText)` 现算。
# 该函数在不同版本的创作平台上可能是同步也可能是 Promise，这里统一 await；
# 拿不到签名函数时直接按无签名发，让平台明确拒绝后回落 RPA（绝不重试直发）。
_REPLAY_SCRIPT = """
async ({url, method, headers, bodyText, sign}) => {
  try {
    const finalHeaders = Object.assign({}, headers || {});
    if (sign) {
      let signed = null;
      try {
        const signer = window._webmsxyw || window.__xhsSigner || window._xyw;
        if (typeof signer === 'function') {
          signed = await signer(url, bodyText);
        } else if (signer && typeof signer.sign === 'function') {
          signed = await signer.sign(url, bodyText);
        }
      } catch (e) { signed = null; }
      if (signed && typeof signed === 'object') {
        const pick = (obj, names) => {
          for (const name of names) {
            if (obj[name] !== undefined && obj[name] !== null && String(obj[name])) {
              return String(obj[name]);
            }
          }
          return '';
        };
        const xs = pick(signed, ['X-s', 'x-s', 'X-S', 'xs']);
        const xt = pick(signed, ['X-t', 'x-t', 'X-T', 'xt']);
        const xsCommon = pick(signed, ['X-s-common', 'x-s-common']);
        const xtCommon = pick(signed, ['X-t-common', 'x-t-common']);
        if (xs) finalHeaders['x-s'] = xs;
        if (xt) finalHeaders['x-t'] = xt;
        if (xsCommon) finalHeaders['x-s-common'] = xsCommon;
        if (xtCommon) finalHeaders['x-t-common'] = xtCommon;
        finalHeaders['x-sign-missing'] = '';
        if (!xs || !xt) {
          return {ok: true, status: 0, payload: null, signMissing: true};
        }
      } else {
        return {ok: true, status: 0, payload: null, signMissing: true};
      }
    }
    const response = await fetch(url, {
      method: method || 'POST',
      headers: finalHeaders,
      credentials: 'include',
      body: bodyText,
    });
    const text = await response.text();
    let payload = null;
    try { payload = JSON.parse(text); } catch (e) { payload = {text: text}; }
    return {ok: true, status: response.status, payload: payload};
  } catch (e) {
    return {ok: false, error: String((e && e.message) ? e.message : e)};
  }
}
"""


async def replay_publish(page, template: dict, body: Any, platform_type: int) -> dict:
    """在页面上下文里直接调用平台创建接口。

    返回 ``{"status": ..., "payload": ..., "post_id": ...}``：

    * ``accepted``  平台确认接收（已创建作品）
    * ``rejected``  平台明确拒绝（可以安全回落 RPA）
    * ``unknown``   结果不确定（绝不能再点一次发布按钮）
    """
    body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    try:
        outcome = await page.evaluate(
            _REPLAY_SCRIPT,
            {
                "url": str(template.get("url") or ""),
                "method": str(template.get("method") or "POST"),
                "headers": template.get("headers") or {},
                "bodyText": body_text,
                "sign": int(platform_type) in SIGNING_PLATFORMS,
            },
        )
    except Exception as exc:
        return {
            "status": "unknown",
            "payload": {},
            "error": f"接口回放异常：{str(exc)[:200]}",
        }
    if not isinstance(outcome, dict) or not outcome.get("ok"):
        message = ""
        if isinstance(outcome, dict):
            message = str(outcome.get("error") or "")
        return {
            "status": "unknown",
            "payload": {},
            "error": message or "接口回放失败",
        }
    if outcome.get("signMissing"):
        # 页面里拿不到 _webmsxyw（版本切换/页面还没加载完）：签名算不出来，
        # 此时按“未尝试直发”回落 RPA，而不是按 unknown 停手（unknown 会记
        # “已提交未验证”，而这里根本还没发出去）。
        return {
            "status": "not_attempted",
            "payload": {},
            "reason": "页面签名函数不可用，已回落页面发布",
        }
    status = int(outcome.get("status") or 0)
    payload = outcome.get("payload")
    # 小红书签名过期时服务端回 HTTP 401/403 + code != 0：明确拒绝，回落 RPA
    # 重走一次页面流程即可拿到新签名，不删除模板（模板本身不含签名）。
    verdict = interpret_response(platform_type, payload, status)
    return {"status": verdict["status"], "payload": payload, **verdict}


def interpret_response(platform_type: int, payload: Any, http_status: int = 200) -> dict:
    """判断平台对创建请求的回应。"""
    platform = int(platform_type)
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    post_id = ""
    raw_status = ""
    if platform == 3:
        code = payload.get("status_code") if isinstance(payload, dict) else None
        if code not in (None, 0, "0"):
            return {"status": "rejected", "post_id": "", "raw_status": str(code)}
        post_id = str(
            data.get("aweme_id") or data.get("awemeId") or data.get("item_id") or ""
        )
        raw_status = str(data.get("status_msg") or data.get("review_result") or "")
        if http_status >= 400 or not post_id:
            return {
                "status": "rejected",
                "post_id": "",
                "raw_status": raw_status or f"HTTP {http_status}",
            }
        return {"status": "accepted", "post_id": post_id, "raw_status": raw_status}
    if platform == 1:
        code = payload.get("code") if isinstance(payload, dict) else None
        if code not in (None, 0, "0"):
            return {"status": "rejected", "post_id": "", "raw_status": str(code)}
        success = payload.get("success") if isinstance(payload, dict) else None
        post_id = str(
            data.get("note_id") or data.get("noteId") or data.get("id") or ""
        )
        raw_status = str(data.get("audit_status") or data.get("status") or "")
        if http_status >= 400 or success is False or not post_id:
            return {
                "status": "rejected",
                "post_id": "",
                "raw_status": raw_status or f"HTTP {http_status}",
            }
        return {"status": "accepted", "post_id": post_id, "raw_status": raw_status}
    if platform == 2:
        code = payload.get("errCode") if isinstance(payload, dict) else None
        base = data.get("baseResp") if isinstance(data.get("baseResp"), dict) else {}
        base_code = base.get("errcode", base.get("errCode"))
        message = str(
            (payload.get("errMsg") if isinstance(payload, dict) else "")
            or base.get("errmsg")
            or base.get("errMsg")
            or ""
        )
        status_text = (message + " " + str(code) + " " + str(base_code)).lower()
        if any(marker in status_text for marker in ("登录", "login", "300330", "300333", "300334")):
            return {"status": "rejected", "post_id": "", "raw_status": message or str(code)}
        if code not in (None, 0, "0") or base_code not in (None, 0, "0"):
            return {
                "status": "rejected", "post_id": "",
                "raw_status": message or str(code or base_code),
            }
        if http_status >= 500 or http_status <= 0:
            return {
                "status": "unknown", "post_id": "",
                "raw_status": message or f"HTTP {http_status}",
            }
        if http_status >= 400:
            return {
                "status": "rejected", "post_id": "",
                "raw_status": message or f"HTTP {http_status}",
            }
        post_id = str(_first_nested_value(
            data,
            ("objectId", "postId", "finderPostId", "exportId", "feedId"),
        ) or "")
        raw_status = message or str(base.get("errmsg") or "")
        if code in (0, "0") or base_code in (0, "0"):
            return {"status": "accepted", "post_id": post_id, "raw_status": raw_status}
        return {
            "status": "unknown", "post_id": post_id,
            "raw_status": raw_status or "平台未返回明确创建结果",
        }
    if http_status >= 400:
        return {"status": "rejected", "post_id": "", "raw_status": f"HTTP {http_status}"}
    return {"status": "accepted", "post_id": "", "raw_status": ""}


# ---------------------------------------------------------------- 录制器

class HybridPublisher:
    """一次发布过程中的"录制 + 直发"助手。

    用法：

    1. ``attach(page)`` 挂在页面开始上传之前；
    2. 上传完成后 ``await attempt(page)`` 尝试直接调创建接口；
    3. 如果返回 ``accepted`` 就不要再点发布按钮；``rejected`` 可以回落 RPA；
       ``unknown`` 必须停手，避免重复发布。
    """

    def __init__(
        self,
        platform_type: int,
        cookie_path: str | Path,
        title: str,
        desc: str = "",
        tags: list[str] | None = None,
        signature: str = "",
        media_file: str | Path = "",
        short_title: str = "",
    ):
        self.platform_type = int(platform_type)
        self.cookie_path = str(cookie_path)
        self.title = str(title or "")
        self.desc = str(desc or "")
        self.tags = [str(tag) for tag in (tags or []) if str(tag).strip()]
        self.signature = str(signature or "")
        self.media_file = str(media_file or "")
        self.short_title = str(short_title or "")
        self.media_ids: dict[str, Any] = {}
        self.template_saved = False
        self._upload_payloads: list[Any] = []
        self._tencent_cover_urls: list[str] = []
        self._file_fingerprint = ""

    # -- 录制 ----------------------------------------------------------

    def attach(self, page) -> None:
        """挂上响应/请求监听，收集素材 ID 并录制创建请求。"""
        page.on("response", self._on_response)
        page.on("request", self._on_request)

    async def _on_response(self, response) -> None:
        url = str(getattr(response, "url", "") or "").lower()
        payload = None
        if self.platform_type == 2 and (
            "/completepartuploaddfs" in url or "/post/post_clip_video" in url
        ):
            try:
                payload = await response.json()
            except Exception:
                payload = None
            self._capture_tencent_response(response, url, payload)
        if not any(marker in url for marker in UPLOAD_URL_MARKERS):
            return
        if payload is None:
            try:
                payload = await response.json()
            except Exception:
                return
        self._upload_payloads.append(payload)
        self.media_ids.update(
            collect_media_ids([payload], self.platform_type)
        )

    async def _on_request(self, request) -> None:
        if self.platform_type == 2:
            await self._capture_tencent_request(request)
        if self.template_saved:
            return
        url = str(getattr(request, "url", "") or "")
        if not is_create_url(self.platform_type, url):
            return
        body_text = getattr(request, "post_data", None)
        if not body_text:
            return
        body, content_type = _parse_request_body(body_text)
        if body is None:
            # 平台改用 multipart 之类的格式时无法安全回放，直接不录模板，
            # 让这次发布走 RPA（下次仍会尝试录制）。
            return
        try:
            headers = dict(request.headers)
        except Exception:
            headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        template = build_template(
            url=url,
            method=str(getattr(request, "method", "POST") or "POST"),
            headers=headers,
            body=body,
            title=self.title,
            desc=self.desc,
            tags=self.tags,
            media_ids=self.media_ids,
            signature=self.signature,
        )
        if save_template(self.cookie_path, self.platform_type, template):
            self.template_saved = True
            logger.info(
                "已录制{}发布模板，下次可直接调用平台接口发布: {}",
                self.platform_type, template_path(self.cookie_path, self.platform_type).name,
            )

    def _capture_tencent_response(self, response, url: str, payload: Any) -> None:
        if payload is None:
            return
        if "/completepartuploaddfs" in url:
            download_url = normalize_tencent_download_url(_first_nested_value(
                payload, ("DownloadURL", "downloadUrl", "downloadURL"),
            ))
            if not download_url:
                return
            try:
                headers = dict(getattr(getattr(response, "request", None), "headers", {}) or {})
            except Exception:
                headers = {}
            filetype = _tencent_filetype(headers)
            if not filetype:
                # 个别 Chromium 版本在跨域重定向后拿不到原请求的 X-Arguments。
                # 视频一定先于封面完成，用响应顺序做保守兜底。
                filetype = (
                    TENCENT_VIDEO_FILETYPE
                    if not self.media_ids.get("video_url")
                    else TENCENT_COVER_FILETYPE
                )
            if filetype == TENCENT_VIDEO_FILETYPE:
                self.media_ids["video_url"] = download_url
            elif filetype == TENCENT_COVER_FILETYPE:
                if download_url not in self._tencent_cover_urls:
                    self._tencent_cover_urls.append(download_url)
                self.media_ids["cover_url"] = self._tencent_cover_urls[0]
                if len(self._tencent_cover_urls) > 1:
                    self.media_ids["share_cover_url"] = self._tencent_cover_urls[1]
            return
        if "/post/post_clip_video" in url:
            draft_id = _first_nested_value(payload, ("draftId", "draftID", "draft_id"))
            clip_key = _first_nested_value(payload, ("clipKey", "clip_key"))
            if draft_id:
                self.media_ids["draft_id"] = str(draft_id)
            if clip_key:
                self.media_ids["clip_key"] = str(clip_key)

    async def _capture_tencent_request(self, request) -> None:
        url = str(getattr(request, "url", "") or "").lower()
        if "/post/post_clip_video" not in url:
            return
        body_text = getattr(request, "post_data", None)
        if not body_text:
            return
        body, _content_type = _parse_request_body(body_text)
        if not isinstance(body, dict):
            return
        video_url = normalize_tencent_download_url(
            body.get("url") or _first_nested_value(body, ("DownloadURL", "downloadUrl")),
        )
        if video_url:
            self.media_ids["video_url"] = video_url
        for source, target in (
            ("width", "video_width"),
            ("height", "video_height"),
            ("fileSize", "file_size"),
        ):
            value = body.get(source)
            if value in (None, ""):
                value = _first_nested_value(body, (source,))
            if value not in (None, ""):
                self.media_ids[target] = value
        duration = body.get("duration")
        if duration in (None, ""):
            duration = _first_nested_value(body, ("duration",))
        if duration not in (None, ""):
            self.media_ids["duration"] = duration

    async def _refresh_media_file_facts(self) -> None:
        if self.platform_type != 2 or not self.media_file:
            return
        path = Path(self.media_file)
        try:
            stat = path.stat()
        except OSError:
            return
        fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
        self.media_ids["file_size"] = int(stat.st_size)
        self.media_ids["upload_cost"] = int((stat.st_size + 524286) // 524287 * 1000)
        if fingerprint == self._file_fingerprint and self.media_ids.get("video_md5"):
            return
        self._file_fingerprint = fingerprint
        try:
            import asyncio

            self.media_ids["video_md5"] = await asyncio.to_thread(_file_md5, path)
        except OSError as exc:
            logger.warning("视频号本地文件 MD5 计算失败（{}）: {}", path.name, exc)

    def _refresh_tencent_derived_values(self) -> None:
        if self._tencent_cover_urls:
            self.media_ids["cover_url"] = self._tencent_cover_urls[0]
            if len(self._tencent_cover_urls) > 1:
                self.media_ids["share_cover_url"] = self._tencent_cover_urls[1]
            else:
                self.media_ids.setdefault("share_cover_url", self._tencent_cover_urls[0])
        try:
            duration = int(self.media_ids.get("duration"))
        except (TypeError, ValueError):
            duration = 0
        if duration > 0:
            self.media_ids["duration_ms"] = duration * 1000

    def _tencent_attempt_values(self) -> dict[str, Any]:
        values = render_values(self.title, self.desc, self.tags)
        for key, value in self.media_ids.items():
            values[f"{{{{media:{key}}}}}"] = value
        values["{{tencent:tags}}"] = [tag.lstrip("#") for tag in self.tags]
        values["{{tencent:short_title_list}}"] = [
            {"shortTitle": self.short_title or self.title}
        ]
        values["{{tencent:timestamp}}"] = str(int(time.time() * 1000))
        values["{{tencent:client_id}}"] = str(uuid.uuid4())
        return values

    def _tencent_requirements_match(self, template: dict) -> tuple[bool, str]:
        requirements = template.get("requirements")
        if not isinstance(requirements, dict):
            return True, ""
        expected_shape = requirements.get("isFullPost")
        actual_shape = _tencent_is_full_post(
            self.media_ids.get("video_width"), self.media_ids.get("video_height"),
        )
        if expected_shape in (0, 1) and actual_shape != expected_shape:
            return False, "视频横竖形态与模板不一致，回落 RPA"
        expected_tags = requirements.get("tagCount")
        if isinstance(expected_tags, int) and len(self.tags) != expected_tags:
            return False, "话题数量与模板不一致，回落 RPA"
        try:
            duration = int(self.media_ids.get("duration"))
        except (TypeError, ValueError):
            duration = 0
        expected_long = bool(requirements.get("longVideo"))
        if duration > 0 and (duration > 60) != expected_long:
            return False, "视频时长跨过模板阈值，回落 RPA"
        return True, ""

    # -- 直发 ----------------------------------------------------------

    async def attempt(self, page) -> dict:
        """尝试用平台接口直接创建作品。

        返回 ``{"status": "accepted"|"rejected"|"unknown"|"not_attempted", ...}``。
        """
        if not hybrid_enabled(self.platform_type):
            return {"status": "not_attempted", "reason": "发布通道为纯 RPA"}
        template = load_template(self.cookie_path, self.platform_type)
        if not template:
            return {"status": "not_attempted", "reason": "尚未录制发布模板"}
        await self._refresh_media_file_facts()
        if self.platform_type == 2:
            self._refresh_tencent_derived_values()
            matches, reason = self._tencent_requirements_match(template)
            if not matches:
                return {"status": "not_attempted", "reason": reason}
        template_signature = str(template.get("signature") or "")
        if template_signature and self.signature and template_signature != self.signature:
            # 模板里带着上次的表单选项（商品链接/合集/声明/封面/定时），
            # 这次选项不一样就不能用旧模板直发，否则会丢掉这次的设置。
            return {
                "status": "not_attempted",
                "reason": "发布选项与模板不一致，回落到 RPA",
            }
        needed = required_media_keys(template)
        if needed and not needed.issubset(set(self.media_ids)):
            missing = sorted(needed - set(self.media_ids))
            return {
                "status": "not_attempted",
                "reason": f"素材 ID 尚未就绪（缺少 {'、'.join(missing)}）",
            }
        if self.platform_type == 2:
            values = self._tencent_attempt_values()
        else:
            values = render_values(self.title, self.desc, self.tags)
            for key, value in self.media_ids.items():
                values[f"{{{{media:{key}}}}}"] = value
        body = render_body(template, values)
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        leftover = [
            placeholder for placeholder in template_placeholders(template)
            if placeholder in text
        ]
        if leftover:
            return {
                "status": "not_attempted",
                "reason": f"模板占位符未解析（{'、'.join(sorted(leftover))}）",
            }
        outcome = await replay_publish(page, template, body, self.platform_type)
        if outcome.get("status") == "accepted":
            logger.info(
                "{} 已通过平台接口直接创建作品: {}",
                self.platform_type, outcome.get("post_id") or "已提交",
            )
        elif outcome.get("status") == "rejected":
            logger.warning(
                "{} 平台接口拒绝了直发，回落到 RPA: {}",
                self.platform_type, outcome.get("raw_status") or "",
            )
        else:
            logger.warning(
                "{} 平台接口结果不确定，不再重复提交: {}",
                self.platform_type, outcome.get("error") or "",
            )
        return outcome


def submitted_result(platform_type: int, reason: str = "") -> dict:
    """结果不确定时的保守结论：已提交，未验证。"""
    return {
        "verified": False,
        "state": "submitted",
        "remote_status": "submitted",
        "post_id": "",
        "verification": "hybrid-api-unknown",
        "transport": "hybrid",
        "note": reason,
    }


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
