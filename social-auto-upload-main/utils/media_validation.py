# -*- coding: utf-8 -*-
"""上传文件白名单与基础文件签名校验。"""
from __future__ import annotations

from pathlib import Path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv", ".wmv", ".mpg", ".mpeg"}
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES
_GENERIC_MIME = {"", "application/octet-stream", "binary/octet-stream"}


class UploadValidationError(ValueError):
    """上传内容不在允许范围内。"""


def _looks_like_media(suffix: str, head: bytes) -> bool:
    suffix = suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if suffix == ".bmp":
        return head.startswith(b"BM")
    if suffix in {".mp4", ".mov", ".m4v"}:
        return b"ftyp" in head[:32]
    if suffix in {".webm", ".mkv"}:
        return head.startswith(b"\x1aE\xdf\xa3")
    if suffix == ".avi":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI "
    if suffix == ".flv":
        return head.startswith(b"FLV")
    if suffix == ".wmv":
        return head.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11")
    if suffix in {".mpg", ".mpeg"}:
        return head.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"))
    return False


def validate_media_upload(file_storage, filename: str | None = None) -> str:
    """校验并返回规范化后缀。

    扩展名、浏览器 MIME 与文件头必须互相兼容。浏览器常用
    ``application/octet-stream`` 标记部分视频，因此该 MIME 允许通过，但仍必须
    命中文件签名；HTML、SVG、脚本等主动内容一律拒绝。
    """
    display_name = filename or getattr(file_storage, "filename", "") or ""
    suffix = Path(display_name).suffix.lower()
    if suffix not in MEDIA_SUFFIXES:
        allowed = ", ".join(sorted(MEDIA_SUFFIXES))
        raise UploadValidationError(f"仅支持图片或视频文件：{allowed}")

    mimetype = str(getattr(file_storage, "mimetype", "") or "").lower()
    expected_prefix = "image/" if suffix in IMAGE_SUFFIXES else "video/"
    if mimetype not in _GENERIC_MIME and not mimetype.startswith(expected_prefix):
        raise UploadValidationError("文件类型与扩展名不匹配")

    stream = getattr(file_storage, "stream", None)
    if stream is None or not hasattr(stream, "read"):
        raise UploadValidationError("无法读取上传文件内容")
    try:
        position = stream.tell()
    except Exception:
        position = 0
    head = stream.read(64) or b""
    try:
        stream.seek(position)
    except Exception as exc:
        raise UploadValidationError("无法校验上传文件") from exc
    if not _looks_like_media(suffix, head):
        raise UploadValidationError("文件内容不是有效的图片或视频")
    return suffix
