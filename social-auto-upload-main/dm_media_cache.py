# -*- coding: utf-8 -*-
"""Cache private-message media beside the local conversation database."""
from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from conf import BASE_DIR

MAX_MESSAGE_MEDIA_BYTES = 80 * 1024 * 1024
NON_MEDIA_CONTENT_TYPES = {
    "application/json", "application/problem+json", "application/xml",
    "text/html", "text/plain",
}


def safe_media_stem(item: dict) -> str:
    raw = str(
        item.get("external_message_id") or item.get("media_url") or "media"
    )
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")[:100]
    if stem:
        return stem
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def download_media(
    url: str,
    directory: str | Path,
    stem: str,
    *,
    referer: str = "",
    max_bytes: int = MAX_MESSAGE_MEDIA_BYTES,
) -> tuple[Path, str]:
    """Download one bounded media file and return its local path and MIME type."""
    url = str(url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("媒体地址格式不正确")
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/*,video/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    try:
        response = requests.get(url, headers=headers, timeout=60, stream=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"下载媒体失败：{str(exc)[:200]}") from exc

    content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
    content_type = content_type.lower()
    if content_type in NON_MEDIA_CONTENT_TYPES or content_type.startswith("text/"):
        response.close()
        raise RuntimeError(f"媒体地址返回了非媒体内容：{content_type or '未知类型'}")
    try:
        content_length = int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        content_length = 0
    if content_length > max_bytes:
        response.close()
        raise RuntimeError(
            f"媒体文件超过 {max_bytes // (1024 * 1024)}MB，已停止下载"
        )
    suffix = mimetypes.guess_extension(content_type) or Path(parsed.path).suffix
    if suffix == ".jpe":
        suffix = ".jpg"
    if not suffix.startswith(".") or len(suffix) > 10:
        suffix = ".bin"
    target = target_dir / f"{stem[:120]}{suffix}"
    total = 0
    try:
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(
                        f"媒体文件超过 {max_bytes // (1024 * 1024)}MB，已停止下载"
                    )
                handle.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        response.close()
    if total <= 0:
        target.unlink(missing_ok=True)
        raise RuntimeError("媒体文件为空")
    return target, content_type


def attach_message_media(
    items: list[dict],
    account: dict,
    skipped: dict[str, int] | None = None,
    *,
    referer: str = "",
) -> dict[str, int]:
    """Download message media and store portable paths relative to ``dm_media``."""
    skipped = skipped if skipped is not None else {}
    account_key = str(account.get("id") or account.get("userName") or "unknown")
    account_key = re.sub(r"[^A-Za-z0-9._-]+", "-", account_key)[:80] or "unknown"
    root = Path(BASE_DIR) / "dm_media"
    target_dir = root / account_key
    downloaded = 0
    failed = 0
    for item in items:
        media_type = str(item.get("media_type") or "")
        url = str(item.get("media_url") or "")
        thumb_url = str(item.get("media_thumb_url") or "")
        if not media_type or (not url and not thumb_url):
            continue
        stem = safe_media_stem(item)
        saved = False
        if url:
            try:
                target, _content_type = download_media(
                    url, target_dir, stem, referer=referer,
                )
                item["media_path"] = target.relative_to(root).as_posix()
                saved = True
            except Exception:
                pass

        # 抖音视频私信通常只暴露封面，不提供原视频 URL。封面本身仍是
        # 有效媒体，应该在页面显示并支持预览，不能因为缺少原视频而丢弃。
        if media_type == "video" and thumb_url and thumb_url != url:
            try:
                thumb, _content_type = download_media(
                    thumb_url, target_dir, stem + "-thumb", referer=referer,
                    max_bytes=8 * 1024 * 1024,
                )
                item["media_thumb_path"] = thumb.relative_to(root).as_posix()
                saved = True
            except Exception:
                pass

        if saved:
            downloaded += 1
        else:
            failed += 1
            skipped["媒体下载失败"] = skipped.get("媒体下载失败", 0) + 1
    return {"downloaded": downloaded, "failed": failed}
