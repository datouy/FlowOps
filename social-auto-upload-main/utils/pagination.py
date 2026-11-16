# -*- coding: utf-8 -*-
"""统一分页参数与响应元数据。"""
from __future__ import annotations

from flask import request


def page_args(default_size: int = 50, max_size: int = 200) -> tuple[int, int, int]:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.args.get("page_size", request.args.get("limit", default_size)))
    except (TypeError, ValueError):
        page_size = default_size
    page_size = max(1, min(page_size, max_size))
    return page, page_size, (page - 1) * page_size


def page_meta(total: int, page: int, page_size: int) -> dict:
    total = max(0, int(total or 0))
    pages = (total + page_size - 1) // page_size if page_size else 0
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
        "has_next": page < pages,
    }
