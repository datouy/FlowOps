# -*- coding: utf-8 -*-
"""本地媒体目录容量统计与可选配额。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from conf import BASE_DIR


def directory_size(path: str | Path) -> int:
    root = Path(path)
    if not root.exists():
        return 0
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                continue
    return total


def quota_bytes() -> int:
    try:
        gib = float(os.environ.get("FLOWOPS_MEDIA_QUOTA_GB", "0") or 0)
    except (TypeError, ValueError):
        gib = 0
    return max(0, int(gib * 1024 ** 3))


def media_usage(base_dir: str | Path | None = None) -> dict:
    base = Path(base_dir or BASE_DIR)
    parts = {
        "videoFile": directory_size(base / "videoFile"),
        "uploadFile": directory_size(base / "uploadFile"),
        "dmMedia": directory_size(base / "dm_media"),
    }
    total = sum(parts.values())
    quota = quota_bytes()
    return {
        "baseDir": str(base),
        "parts": parts,
        "usedBytes": total,
        "quotaBytes": quota,
        "remainingBytes": max(0, quota - total) if quota else None,
        "quotaPercent": round(total * 100 / quota, 1) if quota else None,
    }


def ensure_media_capacity(additional_bytes: int, base_dir: str | Path | None = None) -> None:
    """超出配置配额时抛 ``ValueError``；配额为 0 表示不限制。"""
    usage = media_usage(base_dir)
    quota = int(usage["quotaBytes"] or 0)
    if quota and int(usage["usedBytes"]) + max(0, int(additional_bytes or 0)) > quota:
        raise ValueError(
            "媒体目录已超过配额：已用 %.1f GB，配额 %.1f GB"
            % (usage["usedBytes"] / 1024 ** 3, quota / 1024 ** 3)
        )
    free = int(shutil.disk_usage(str(Path(base_dir or BASE_DIR))).free)
    if free - max(0, int(additional_bytes or 0)) < 256 * 1024 * 1024:
        raise ValueError("磁盘剩余空间不足 256MB，已拒绝写入")


def storage_overview(base_dir: str | Path | None = None) -> dict:
    base = Path(base_dir or BASE_DIR)
    usage = media_usage(base)
    db_files = {}
    for path in (base / "db").glob("*.db"):
        try:
            db_files[path.name] = path.stat().st_size
        except OSError:
            continue
    disk = shutil.disk_usage(str(base))
    usage["databases"] = db_files
    usage["databaseBytes"] = sum(db_files.values())
    usage["diskFreeBytes"] = int(disk.free)
    usage["diskTotalBytes"] = int(disk.total)
    return usage
