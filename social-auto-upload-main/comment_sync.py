# -*- coding: utf-8 -*-
"""评论自动同步与回复草稿。

原来评论只能靠人工从平台页面复制、批量导入（``ops_center.add_messages``），
运营量一大就漏。这里补上自动同步：

- 打开各平台创作者中心的评论管理页，拦截评论列表接口；
- 解析成统一的评论结构，写进 ``messages`` 表（``msg_type='comment'``）；
- 和私信共用回复规则引擎，命中规则的评论生成待发送的回复草稿。

平台没有开放的评论列表接口时退回"打开页面抓取"的方式，
抓不到就明确返回"不支持"，不静默失败。
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

import runtime_guard
from conf import BASE_DIR
from utils.credential_crypto import resolve_credential_path

# 各平台创作者中心的评论管理页。
COMMENT_PAGES: dict[int, str] = {
    1: "https://creator.xiaohongshu.com/creator/notes",
    2: "https://channels.weixin.qq.com/platform/post/comment",
    3: "https://creator.douyin.com/creator-micro/content/manage",
    4: "https://cp.kuaishou.com/article/manage/video",
}

# 平台编号 -> 平台中文名（与 user_info.type 一致）。
PLATFORM_NAMES = {1: "小红书", 2: "视频号", 3: "抖音", 4: "快手"}

# 评论同步支持的平台（覆盖全部已接入的平台）。
SUPPORTED_PLATFORMS = tuple(sorted(COMMENT_PAGES))

# 评论列表接口的路径特征（用于拦截响应）。
COMMENT_RESPONSE_MARKERS: dict[int, tuple[str, ...]] = {
    1: ("/api/galaxy/creator/comment", "/web_api/sns/v2/comment", "/creator/comment"),
    2: ("/post/comment", "/comment/list"),
    3: ("/janus/douyin/creator/pc/comment", "/comment/list"),
}

# 一次同步最多取多少条评论。
MAX_COMMENTS_PER_ACCOUNT = 100
# 同步间隔（秒）。
DEFAULT_INTERVAL = 900

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "last_run_at": "",
    "last_error": "",
    "synced": 0,
}


# ---------------------------------------------------------------- 解析

def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _first_value(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def extract_comments(payload: Any) -> list[dict]:
    """把评论列表接口的响应解析成统一结构。"""
    comments: list[dict] = []
    seen: set[str] = set()
    for item in _walk_dicts(payload):
        content = _first_value(item, ("content", "text", "comment_content", "comment"))
        if not content:
            continue
        comment_id = _first_value(
            item, ("comment_id", "commentId", "cid", "id"),
        )
        if not comment_id:
            fingerprint = "|".join(
                (content, _first_value(item, ("nickname", "user_name", "author")),
                 _first_value(item, ("create_time", "createTime", "time")))
            )
            comment_id = "c-" + str(abs(hash(fingerprint)))
        if comment_id in seen:
            continue
        seen.add(comment_id)
        comments.append({
            "external_comment_id": comment_id,
            "author": _first_value(
                item, ("nickname", "user_name", "userName", "author", "name"),
            ),
            "content": content[:2000],
            "created_at": _first_value(
                item, ("create_time", "createTime", "publish_time", "time"),
            ),
            "like_count": _first_value(item, ("like_count", "likeCount", "digg_count")),
            "reply_count": _first_value(item, ("reply_count", "replyCount")),
            "source_title": _first_value(
                item, ("note_title", "title", "video_title", "aweme_title"),
            ),
            "source_id": _first_value(
                item, ("note_id", "aweme_id", "item_id", "object_id", "post_id"),
            ),
        })
    return comments


# ---------------------------------------------------------------- 抓取

async def _collect_comments_in_page(page, platform_type: int) -> list[dict]:
    markers = COMMENT_RESPONSE_MARKERS.get(int(platform_type), ())
    captured: list[Any] = []

    def on_response(response):
        url = str(getattr(response, "url", "") or "")
        if any(marker in url for marker in markers):
            captured.append(response)

    page.on("response", on_response)
    try:
        url = COMMENT_PAGES.get(int(platform_type))
        if not url:
            raise RuntimeError("该平台暂不支持评论自动同步")
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await asyncio.sleep(8)
        if "login" in str(page.url or "").lower():
            raise RuntimeError("登录已失效，请重新扫码登录")
        payloads = []
        for response in captured[-40:]:
            try:
                payloads.append(await response.json())
            except Exception:
                continue
        comments: list[dict] = []
        for payload in payloads:
            comments.extend(extract_comments(payload))
        # 去重并截断。
        unique: dict[str, dict] = {}
        for comment in comments:
            unique.setdefault(comment["external_comment_id"], comment)
        return list(unique.values())[:MAX_COMMENTS_PER_ACCOUNT]
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


async def _collect_comments(account: dict, platform_type: int) -> list[dict]:
    from patchright.async_api import async_playwright

    from utils.account_browser import open_persistent_account_context

    async with async_playwright() as playwright:
        context = await open_persistent_account_context(
            playwright,
            account["cookie_path"],
            account_id=(account or {}).get("id"),
            headless=True,
            channel="chromium",
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        try:
            page = await context.new_page()
            return await _collect_comments_in_page(page, platform_type)
        finally:
            await context.close()


def fetch_comments(account: dict, platform_type: int) -> list[dict]:
    """抓取一个账号的评论（同步封装）。"""
    return asyncio.run(_collect_comments(account, platform_type))


# ---------------------------------------------------------------- 落库

def upsert_comment_items(
    comments: list[dict], account: dict, platform_type: int,
) -> int:
    """把评论的平台标识写进 ``comment_items`` 表。

    这张表是评论**可操作**的前提：回复 / 点赞 / 删除 / 置顶都需要平台侧的
    ``comment_id`` 与 ``source_id``（所属作品 id），``messages`` 表里没有这两
    个字段，只靠同步草稿无法完成任何写操作。
    """
    if not comments:
        return 0
    platform_name = PLATFORM_NAMES.get(int(platform_type), "")
    account_name = str(account.get("userName") or "")
    account_id = account.get("id")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    saved = 0
    try:
        import dm_ops

        with dm_ops.get_conn() as conn:
            for comment in comments:
                comment_id = str(comment.get("external_comment_id") or "").strip()
                if not comment_id:
                    continue
                conn.execute(
                    """INSERT INTO comment_items (
                        platform, account, account_id, comment_id, source_id,
                        source_title, author, content, created_at,
                        like_count, reply_count, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(platform, account, comment_id) DO UPDATE SET
                        source_id=excluded.source_id,
                        source_title=excluded.source_title,
                        author=excluded.author,
                        content=excluded.content,
                        like_count=excluded.like_count,
                        reply_count=excluded.reply_count,
                        updated_at=excluded.updated_at""",
                    (
                        platform_name, account_name,
                        int(account_id) if account_id else None,
                        comment_id,
                        str(comment.get("source_id") or ""),
                        str(comment.get("source_title") or ""),
                        str(comment.get("author") or ""),
                        str(comment.get("content") or ""),
                        str(comment.get("created_at") or ""),
                        _to_int(comment.get("like_count")),
                        _to_int(comment.get("reply_count")),
                        now,
                    ),
                )
                saved += 1
    except Exception as exc:
        logger.warning("评论运营表写入失败: {}", exc)
        return 0
    return saved


def _to_int(value: Any) -> int:
    try:
        return int(str(value or "").strip() or 0)
    except (TypeError, ValueError):
        return 0


def _account_rows(platform_type: int) -> list[dict]:
    from db_utils import connect

    db_main = Path(BASE_DIR) / "db" / "database.db"
    if not db_main.exists():
        return []
    with connect(db_main, timeout=15) as conn:
        rows = [
            dict(row) for row in conn.execute(
                "SELECT id,type,userName,filePath,status FROM user_info "
                "WHERE type=? ORDER BY id",
                (int(platform_type),),
            ).fetchall()
        ]
    cookie_dir = Path(BASE_DIR) / "cookiesFile"
    result = []
    for row in rows:
        cookie_path = cookie_dir / Path(str(row.get("filePath") or "")).name
        # 凭据可能是明文也可能是密文，两种都要认。
        resolved = resolve_credential_path(cookie_path)
        if resolved is None or resolved.stat().st_size <= 2:
            continue
        row["cookie_path"] = str(resolved)
        result.append(row)
    return result


def _comment_items(comments: list[dict], account: dict, platform_type: int) -> list[dict]:
    platform_name = {1: "小红书", 2: "视频号", 3: "抖音"}.get(int(platform_type), "")
    account_name = str(account.get("userName") or "")
    items = []
    for comment in comments:
        items.append({
            "platform": platform_name,
            "account": account_name,
            "msg_type": "comment",
            "author": comment.get("author") or "",
            "content": comment.get("content") or "",
            "source_content": comment.get("source_title") or "",
            "external_message_id": comment.get("external_comment_id") or "",
            "created_at": comment.get("created_at") or "",
            "direction": "inbound",
            "delivery_status": "received",
            "status": "new",
            "intent": "low",
        })
    return items


def sync_account_comments(account: dict, platform_type: int) -> dict:
    """同步一个账号的评论并生成回复草稿。"""
    comments = fetch_comments(account, platform_type)
    if not comments:
        return {"account": account.get("userName") or "", "comments": 0, "imported": 0}
    items = _comment_items(comments, account, platform_type)
    # 先落运营表（带平台标识），再入 messages：顺序反过来的话，
    # 写操作会指向一条还没登记的评论。
    tracked = upsert_comment_items(comments, account, platform_type)
    try:
        import dm_ops

        ingest = dm_ops.ingest_many(items, allow_auto_reply=True, force_draft=True)
    except Exception as exc:
        logger.warning("评论入库失败: {}", exc)
        return {
            "account": account.get("userName") or "",
            "comments": len(comments),
            "imported": 0,
            "tracked": tracked,
            "error": str(exc)[:300],
        }
    ready = sum(
        1 for result in ingest.get("items", [])
        if (result.get("auto_reply") or {}).get("status") == "ready"
    )
    return {
        "account": account.get("userName") or "",
        "comments": len(comments),
        "imported": int(ingest.get("added") or 0),
        "duplicates": int(ingest.get("duplicates") or 0),
        "tracked": tracked,
        "auto_replies_ready": ready,
    }


def run_comment_sync_once() -> dict:
    """同步所有已登录账号的评论。"""
    with _lock:
        _state.update(running=True, last_error="")
    result: dict[str, Any] = {"platforms": {}, "synced": 0, "errors": []}
    try:
        for platform_type in SUPPORTED_PLATFORMS:
            accounts = _account_rows(platform_type)
            platform_result = []
            for account in accounts:
                try:
                    platform_result.append(
                        sync_account_comments(account, platform_type)
                    )
                except Exception as exc:
                    message = str(exc)[:300] or exc.__class__.__name__
                    logger.warning(
                        "评论同步失败（{} / {}）: {}", platform_type,
                        account.get("userName"), message,
                    )
                    result["errors"].append({
                        "platform": platform_type,
                        "account": account.get("userName") or "",
                        "error": message,
                    })
            result["platforms"][str(platform_type)] = platform_result
            result["synced"] += sum(
                int(item.get("imported") or 0) for item in platform_result
            )
    finally:
        with _lock:
            _state.update(
                running=False,
                last_run_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
    return result


def comment_status() -> dict:
    with _lock:
        return dict(_state)


# ---------------------------------------------------------------- 后台线程

_comment_loop_started = False
_comment_loop_guard = threading.Lock()


def start_comment_loop(interval_seconds: int = DEFAULT_INTERVAL) -> None:
    """启动评论自动同步的后台线程（幂等）。

    注意：这个方法以前**没有**幂等保护，被调用两次就会跑两个同步线程——
    两个线程同时拉同一批评论，重复入库、互相抢游标。看门狗重启线程之后
    这个隐患会从"潜在"变成"必然"，所以顺手补上。
    """
    global _comment_loop_started
    with _comment_loop_guard:
        if _comment_loop_started:
            return
        _comment_loop_started = True

    def _loop():
        runtime_guard.beat('comment-sync')
        _generation = runtime_guard.current_generation('comment-sync')
        while True:
            if not runtime_guard.is_current('comment-sync', _generation):
                return
            try:
                run_comment_sync_once()
                runtime_guard.beat('comment-sync')
            except Exception as exc:
                runtime_guard.beat('comment-sync', ok=False, error=str(exc))
                logger.exception("评论自动同步异常")
            time.sleep(max(60, int(interval_seconds or DEFAULT_INTERVAL)))

    threading.Thread(target=_loop, name="comment-sync", daemon=True).start()


def restart_comment_loop(interval_seconds: int = DEFAULT_INTERVAL) -> None:
    """线程死掉后重新拉起评论同步（看门狗用）。"""
    global _comment_loop_started
    with _comment_loop_guard:
        _comment_loop_started = False
    logger.warning("重新拉起评论同步线程")
    start_comment_loop(interval_seconds)
