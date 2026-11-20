# -*- coding: utf-8 -*-
"""异步任务执行器注册。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import task_jobs


@task_jobs.runner("ai.content")
def run_ai_content(params, progress, cancelled):
    import ai_content

    if cancelled():
        raise task_jobs.TaskCancelled()
    progress(5, "正在生成文案")
    result = ai_content.generate_content(
        params.get("data") or {},
        caller=str(params.get("caller") or "local"),
    )
    progress(95, "文案生成完成")
    return result


@task_jobs.runner("ai.content_pack")
def run_ai_pack(params, progress, cancelled):
    import ai_content

    if cancelled():
        raise task_jobs.TaskCancelled()

    def report(value, message=""):
        progress(value, message)
        if cancelled():
            raise task_jobs.TaskCancelled()

    return ai_content.generate_pack(
        params.get("data") or {},
        progress_cb=report,
        caller=str(params.get("caller") or "local"),
    )


@task_jobs.runner("accounts.check_all")
def run_account_check(params, progress, cancelled):
    from account_store import get_store
    from myUtils.auth import check_cookies_concurrently
    from utils.account_serial import account_key
    import account_safety

    base_dir = Path(str(params.get("baseDir")))
    store = get_store(base_dir)
    accounts = list(params.get("accounts") or [])
    total = max(1, len(accounts))
    results = []
    ok = 0
    bad = 0

    for offset in range(0, len(accounts), 4):
        if cancelled():
            raise task_jobs.TaskCancelled()
        batch = accounts[offset: offset + 4]
        pending = []
        for item in batch:
            account_id = int(item.get("id") or 0)
            cookie_name = str(item.get("cookie") or "")
            live_account = store.get(account_id) or item
            if account_safety.is_paused(live_account):
                results.append({
                    "id": account_id,
                    "name": item.get("name") or live_account.get("name") or "",
                    "alive": False,
                    "status": live_account.get("status"),
                    "reason": "自动动作已暂停，未访问平台",
                    "paused": True,
                })
                continue
            if not item.get("cookieExists"):
                store.mark_status(account_id, 0, "凭据文件缺失，请重新扫码授权")
                bad += 1
                fresh = store.get(account_id) or item
                results.append({
                    "id": account_id,
                    "name": item.get("name") or "",
                    "alive": False,
                    "status": fresh.get("status"),
                    "reason": fresh.get("failReason") or "凭据文件缺失",
                })
                continue
            platform_type = int(item.get("type") or 0)
            pending.append((
                account_id,
                platform_type,
                cookie_name,
                item,
                account_key(
                    platform_type,
                    live_account.get("name") or item.get("name"),
                    account_id,
                    cookie_name,
                ),
            ))
        if not pending:
            progress(int(len(results) * 100 / total), "账号检查中")
            continue
        flags = asyncio.run(check_cookies_concurrently(
            [
                (platform_type, cookie, lease_key)
                for _id, platform_type, cookie, _item, lease_key in pending
            ],
            max_concurrent=4,
        ))
        for index, (
            account_id, _platform_type, _cookie, item, _lease_key,
        ) in enumerate(pending):
            alive = bool(flags.get(index, False))
            store.mark_checked(
                account_id,
                alive,
                "" if alive else "登录态失效，请重新授权",
            )
            if alive:
                ok += 1
            else:
                bad += 1
            fresh = store.get(account_id) or item
            results.append({
                "id": account_id,
                "name": item.get("name") or fresh.get("name") or "",
                "alive": alive,
                "status": fresh.get("status"),
                "reason": fresh.get("failReason") or "",
            })
        progress(
            min(95, int(len(results) * 100 / total)),
            f"已检查 {len(results)}/{len(accounts)} 个账号",
        )
    return {"ok": ok, "bad": bad, "total": len(results), "accounts": results}


@task_jobs.runner("comments.sync")
def run_comments_sync(params, progress, cancelled):
    import comment_sync

    if cancelled():
        raise task_jobs.TaskCancelled()
    progress(10, "正在同步平台评论")
    result = comment_sync.run_comment_sync_once()
    if cancelled():
        raise task_jobs.TaskCancelled()
    progress(95, "评论同步完成")
    return result


@task_jobs.runner("dm.reply")
def run_dm_reply(params, progress, cancelled):
    import dm_ops

    if cancelled():
        raise task_jobs.TaskCancelled()
    progress(10, "正在发送私信")
    result = dm_ops._manual_reply_payload(
        int(params.get("contactId") or 0),
        str(params.get("text") or ""),
        guard=False,
    )
    if cancelled():
        raise task_jobs.TaskCancelled()
    progress(95, "私信发送完成")
    return result


@task_jobs.runner("dm.ai_draft")
def run_dm_ai_draft(params, progress, cancelled):
    import dm_ops

    if cancelled():
        raise task_jobs.TaskCancelled()
    progress(10, "正在生成回复草稿")
    result = dm_ops._ai_draft_payload(
        int(params.get("contactId") or 0),
        guard=False,
    )
    if cancelled():
        raise task_jobs.TaskCancelled()
    progress(95, "回复草稿生成完成")
    return result
