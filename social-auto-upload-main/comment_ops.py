# -*- coding: utf-8 -*-
"""评论运营能力：回复 / 点赞 / 删除 / 置顶。

为什么单独一个模块
------------------
``comment_sync.py`` 之前只做一件事：**把评论读进来**。读完之后运营想做任何
动作（回复、点赞、删除、置顶）都得自己去平台页面点一遍——工具的价值只兑现
了一半。

这里把"评论操作"收敛成一个统一入口：

    run_comment_action(account, comment, action, text="")

四个动作对上层是同一套语义，差异（视频号有 API、其它平台只能走页面自动化）
藏在各自适配器里。

平台能力矩阵
------------
=========  ====  ====  ====  ====
平台        回复  点赞  删除  置顶
=========  ====  ====  ====  ====
视频号      API   API   API   API
抖音        页面  页面  页面  页面
小红书      页面  页面  页面  页面
快手        页面  页面  页面  页面
=========  ====  ====  ====  ====

视频号走 ``channels.weixin.qq.com`` 的正式接口（已确认的
``comment/create_comment``、``comment/like_comment``、``comment/del_comment``、
``comment/set_top_comment``），带账号指纹 UA 和账号代理，链路最稳。

其余平台没有公开可直连的写接口，用浏览器页面自动化：定位到评论所在的作品
→ 定位评论行 → 点对应按钮。**定位不到就明确报错**，绝不静默"成功"——
运营需要知道哪条评论没操作成功。

数据契约
--------
操作需要两个平台侧标识，``comment_sync`` 采集时必须一起带出来：

- ``comment_id`` —— 评论在平台上的 id
- ``source_id`` —— 评论所属作品（笔记 / 视频）的 id

少了任何一个都无法在平台上定位这条评论，操作会被明确拒绝。
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from utils import browser_timeout

# ---------------------------------------------------------------- 动作定义

ACTION_REPLY = "reply"
ACTION_LIKE = "like"
ACTION_UNLIKE = "unlike"
ACTION_DELETE = "delete"
ACTION_PIN = "pin"
ACTION_UNPIN = "unpin"

# 所有受支持的评论动作。调用方（接口层）只允许传这里列出的值。
COMMENT_ACTIONS = (
    ACTION_REPLY, ACTION_LIKE, ACTION_UNLIKE,
    ACTION_DELETE, ACTION_PIN, ACTION_UNPIN,
)

# 平台上按钮的文案关键词。不同平台的按钮文案不完全一致，这里按"包含"匹配。
ACTION_BUTTON_KEYWORDS: dict[str, tuple[str, ...]] = {
    ACTION_REPLY: ("回复", "respond", "reply"),
    ACTION_LIKE: ("点赞", "赞", "like", "digg"),
    ACTION_DELETE: ("删除", "delete", "remove"),
    ACTION_PIN: ("置顶", "top", "pin", "sticky"),
    ACTION_UNPIN: ("取消置顶", "unpin", "取消顶"),
}

# 平台中文名 -> 平台编号（与 user_info.type 一致）。
PLATFORM_NAMES = {1: "小红书", 2: "视频号", 3: "抖音", 4: "快手", 5: "微博", 6: "百家号"}
PLATFORM_TYPES = {name: key for key, name in PLATFORM_NAMES.items()}

# 各平台创作者中心的评论管理页。
COMMENT_PAGES: dict[int, str] = {
    1: "https://creator.xiaohongshu.com/creator/notes",
    2: "https://channels.weixin.qq.com/platform/post/comment",
    3: "https://creator.douyin.com/creator-micro/content/manage",
    4: "https://cp.kuaishou.com/article/manage/video",
}

# 单个动作的执行超时（秒）。
ACTION_TIMEOUT = 90


class CommentActionError(RuntimeError):
    """评论操作失败（平台未开放、定位失败、平台返回错误等）。"""


class CommentNotSupported(CommentActionError):
    """该平台不支持这个动作。"""


_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def platform_type_of(value: Any) -> int:
    """把平台名 / 平台编号统一成平台编号（与 ``user_info.type`` 一致）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    text = str(value or "").strip()
    if text in PLATFORM_TYPES:
        return PLATFORM_TYPES[text]
    for key, name in PLATFORM_NAMES.items():
        if name == text:
            return key
    return 0


# ---------------------------------------------------------------- 视频号适配器

class ChannelsCommentAdapter:
    """视频号：走创作者后台正式接口。

接口路径来自已确认的 ``Shipinhao.*`` 能力面，
    复用 ``ChannelsClient`` 的会话——它已经带上了账号指纹 UA 与账号代理，
    直连接口与浏览器通道因此保持同一个设备身份和同一个出口 IP。
    """

    platform_type = 2
    supports = frozenset(COMMENT_ACTIONS)

    INTERACTION_API_BASE = (
        "https://channels.weixin.qq.com"
        "/micro/interaction/cgi-bin/mmfinderassistant-bin"
    )

    def __init__(self, account: dict):
        self.account = account

    def _client(self):
        from channels_dm import ChannelsClient

        return ChannelsClient(self.account)

    @staticmethod
    def _comment_body(comment: dict, extra: dict | None = None) -> dict:
        body = {
            "objectId": str(comment.get("source_id") or "").strip(),
            "objectDesc": str(comment.get("source_title") or "").strip(),
            "commentId": str(comment.get("comment_id") or "").strip(),
            "commentIdStr": str(comment.get("comment_id") or "").strip(),
            "nickname": str(comment.get("author") or "").strip(),
            "content": str(comment.get("content") or "").strip(),
        }
        body.update(extra or {})
        return body

    def _require_source(self, comment: dict) -> None:
        if not str(comment.get("source_id") or "").strip():
            raise CommentActionError(
                "这条评论缺少所属作品 ID，无法在平台上定位。"
                "请先重新同步一次评论再操作。"
            )

    def reply(self, client, comment: dict, text: str) -> dict:
        self._require_source(comment)
        body = self._comment_body(comment, {"replyContent": text, "content": text})
        client._request(
            "/comment/create_comment", body, self.INTERACTION_API_BASE,
        )
        return {"ok": True, "action": ACTION_REPLY, "transport": "channels-api"}

    def like(self, client, comment: dict, liked: bool = True) -> dict:
        self._require_source(comment)
        body = self._comment_body(comment, {"like": 1 if liked else 0})
        client._request(
            "/comment/like_comment", body, self.INTERACTION_API_BASE,
        )
        return {
            "ok": True,
            "action": ACTION_LIKE if liked else ACTION_UNLIKE,
            "transport": "channels-api",
        }

    def delete(self, client, comment: dict) -> dict:
        self._require_source(comment)
        client._request(
            "/comment/del_comment", self._comment_body(comment),
            self.INTERACTION_API_BASE,
        )
        return {"ok": True, "action": ACTION_DELETE, "transport": "channels-api"}

    def pin(self, client, comment: dict, pinned: bool = True) -> dict:
        self._require_source(comment)
        body = self._comment_body(comment, {"top": 1 if pinned else 0})
        client._request(
            "/comment/set_top_comment", body, self.INTERACTION_API_BASE,
        )
        return {
            "ok": True,
            "action": ACTION_PIN if pinned else ACTION_UNPIN,
            "transport": "channels-api",
        }


# ---------------------------------------------------------------- 页面自动化

async def _locate_comment_row(page, comment: dict):
    """在评论管理页里定位到目标评论行。

    定位顺序：

    1. 先按作者昵称精确匹配评论行（评论区每行都有昵称）；
    2. 昵称匹配不到时退化为"包含关系"；
    3. 仍然匹配不到，尝试用评论正文前缀匹配。

    找不到时抛 ``CommentActionError``——不猜、不乱点。
    """
    author = str(comment.get("author") or "").strip()
    content = str(comment.get("content") or "").strip()
    # 评论行的常见容器：带作者昵称的块。
    rows = await page.evaluate(
        """() => {
            const nodes = [...document.querySelectorAll(
                'li,div,tr,[class*=comment],[class*=Comment],[class*=reply],[class*=Reply]'
            )];
            const out = [];
            for (const node of nodes) {
                const text = (node.innerText || '').trim();
                if (!text || text.length > 2000) continue;
                const nick = node.querySelector(
                    '[class*=nickname],[class*=nickName],[class*=name],[class*=author]'
                );
                const nickText = ((nick && nick.innerText) || '').trim();
                if (!nickText) continue;
                // 只取最内层的节点，避免把整个评论区当成一行。
                if (nodes.some(other => other !== node && node.contains(other)
                        && ((other.innerText || '').trim() === text))) continue;
                out.push({nickname: nickText, text: text});
            }
            return out;
        }"""
    )
    if not rows:
        raise CommentActionError("评论管理页没有加载出评论列表，请确认登录状态")
    if author:
        for row in rows:
            if row["nickname"] == author:
                return row
        for row in rows:
            if author and (author in row["nickname"] or row["nickname"] in author):
                return row
    if content:
        prefix = content[:12]
        for row in rows:
            if prefix and prefix in row["text"]:
                return row
    raise CommentActionError(
        f"没有在评论列表里找到这条评论（作者：{author or '未知'}），"
        "可能已被删除或不在当前筛选结果里"
    )


async def _click_action(page, row_text: str, action: str) -> bool:
    """在评论行内点击某个动作按钮。返回是否点到了。"""
    keywords = ACTION_BUTTON_KEYWORDS.get(action, ())
    if not keywords:
        return False
    clicked = await page.evaluate(
        """([rowText, keywords]) => {
            const nodes = [...document.querySelectorAll(
                'button,[role=button],a,span,i,svg'
            )];
            const isRow = (node) => {
                let cur = node;
                for (let i = 0; i < 8 && cur; i += 1) {
                    const text = (cur.innerText || '').trim();
                    if (text && text.length <= 3000 && rowText &&
                            text.includes(rowText.slice(0, 40))) return true;
                    cur = cur.parentElement;
                }
                return false;
            };
            for (const node of nodes) {
                const label = [
                    node.getAttribute('title') || '',
                    node.getAttribute('aria-label') || '',
                    (node.innerText || '').trim(),
                ].join(' ').toLowerCase();
                if (!label) continue;
                if (!keywords.some(key => label.includes(key.toLowerCase()))) continue;
                if (!isRow(node)) continue;
                node.scrollIntoView({block: 'center'});
                node.click();
                return true;
            }
            return false;
        }""",
        [row_text, list(keywords)],
    )
    return bool(clicked)


async def _dom_reply(page, row_text: str, text: str) -> None:
    """点开回复输入框并提交。"""
    if not await _click_action(page, row_text, ACTION_REPLY):
        raise CommentActionError("没有找到回复按钮，平台页面结构可能已改版")
    await page.wait_for_timeout(800)
    editor = None
    for selector in (
        "textarea",
        "[contenteditable=true]",
        "[class*=input][class*=reply]",
        "[class*=editor]",
        "[class*=Input]",
    ):
        try:
            locator = page.locator(selector).last
            if locator.count():
                editor = locator
                break
        except Exception:
            continue
    if editor is None:
        raise CommentActionError("点击回复后没有出现输入框，平台页面结构可能已改版")
    try:
        editor.click()
        editor.fill(text)
        page.wait_for_timeout(300)
        editor.press("Enter")
    except Exception as exc:
        raise CommentActionError(f"回复内容提交失败：{str(exc)[:200]}") from exc
    await page.wait_for_timeout(1200)


class DomCommentAdapter:
    """通用页面自动化适配器（抖音 / 小红书 / 快手）。"""

    supports = frozenset(COMMENT_ACTIONS)

    def __init__(self, account: dict, platform_type: int):
        self.account = account
        self.platform_type = int(platform_type)

    async def _run(self, action: str, comment: dict, text: str = "", capture=None) -> dict:
        from patchright.async_api import async_playwright

        from utils.account_browser import open_persistent_account_context

        validate_comment(comment, action)
        cookie_path = str(self.account.get("cookie_path") or "")
        if not cookie_path or not Path(cookie_path).is_file():
            raise CommentActionError(
                "账号的 Cookie 文件不存在，请重新扫码登录后再操作评论"
            )
        url = COMMENT_PAGES.get(self.platform_type)
        if not url:
            raise CommentNotSupported(
                f"{PLATFORM_NAMES.get(self.platform_type, '该平台')}暂不支持评论操作"
            )
        async with async_playwright() as playwright:
            context = await open_persistent_account_context(
                playwright,
                cookie_path,
                account_id=(self.account or {}).get("id"),
                headless=True,
                channel="chromium",
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            if capture is not None:
                try:
                    capture(context)
                except Exception:
                    pass
            try:
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=ACTION_TIMEOUT * 1000)
                await page.wait_for_timeout(3000)
                lowered = str(page.url or "").lower()
                if any(marker in lowered for marker in ("/login", "passport")):
                    raise CommentActionError("登录已失效，请重新扫码登录")
                row = await _locate_comment_row(page, comment)
                if action == ACTION_REPLY:
                    await _dom_reply(page, row["text"], text)
                    return {"ok": True, "action": action, "transport": "dom"}
                if not await _click_action(page, row["text"], action):
                    raise CommentActionError(
                        f"没有找到「{action}」按钮，平台页面结构可能已改版"
                    )
                await page.wait_for_timeout(1500)
                # 删除类操作通常有二次确认。
                if action == ACTION_DELETE:
                    await _confirm_dialog(page)
                await page.wait_for_timeout(1000)
                return {"ok": True, "action": action, "transport": "dom"}
            finally:
                await browser_timeout.close_async_resource_async(
                    context, label="关闭评论操作浏览器",
                )

    def _call(self, action: str, comment: dict, text: str = "") -> dict:
        # 评论操作同样跑在账号 Profile 锁内部：必须有总超时，且清理动作
        # （关闭浏览器）也不能再变成第二个卡点。超时后还要强杀浏览器，
        # 让卡住的线程走到 finally 释放 Profile 锁。
        return browser_timeout.run_browser_query(
            lambda capture: self._run(action, comment, text, capture=capture),
            timeout=ACTION_TIMEOUT,
            label=f"评论操作（{action}）",
        )


async def _confirm_dialog(page) -> None:
    """点掉删除确认弹窗（存在才点）。"""
    for selector in (
        "button:has-text('确定')",
        "button:has-text('确认')",
        "button:has-text('删除')",
        "[class*=confirm] button",
        ".el-message-box button.el-button--primary",
    ):
        try:
            locator = page.locator(selector).last
            if locator.count() and locator.is_visible():
                locator.click()
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue


# ---------------------------------------------------------------- 统一入口

def _adapter(account: dict, platform_type: int):
    if platform_type == 2:
        return ChannelsCommentAdapter(account)
    return DomCommentAdapter(account, platform_type)


def validate_comment(comment: dict, action: str = "") -> None:
    """操作前的必填校验——**在任何网络访问之前**跑。

    平台侧的评论 ID 与作品 ID 是定位评论的唯一依据；缺了就必须在发请求
    之前拒绝，否则要等到打开浏览器、登录、翻页之后才报错，白白消耗账号的
    风控额度。
    """
    if not str(comment.get("comment_id") or "").strip():
        raise CommentActionError(
            "这条评论缺少平台评论 ID，无法操作。请先重新同步一次评论。"
        )
    if not str(comment.get("source_id") or "").strip():
        raise CommentActionError(
            "这条评论缺少所属作品 ID，无法在平台上定位。"
            "请先重新同步一次评论再操作。"
        )


def run_comment_action(
    account: dict,
    comment: dict,
    action: str,
    text: str = "",
) -> dict:
    """执行一个评论动作。

    ``comment`` 至少要有 ``comment_id`` 与 ``source_id``（所属作品 id），
    ``author`` 用于在页面上定位评论行。

    返回 ``{"ok": True, "action": ..., "transport": ...}``；
    失败抛 :class:`CommentActionError`，由接口层转成 4xx/5xx。
    """
    action = str(action or "").strip().lower()
    if action not in COMMENT_ACTIONS:
        raise CommentActionError(f"不支持的评论操作：{action}")
    if action == ACTION_REPLY and not str(text or "").strip():
        raise CommentActionError("回复内容不能为空")
    validate_comment(comment, action)

    platform_type = platform_type_of(
        comment.get("platform") or account.get("platform") or account.get("type")
    )
    if not platform_type:
        raise CommentActionError("无法识别评论所属平台")

    adapter = _adapter(account, platform_type)
    if action not in adapter.supports:
        raise CommentNotSupported(
            f"{PLATFORM_NAMES.get(platform_type, '该平台')}暂不支持「{action}」操作"
        )

    with _lock:
        if isinstance(adapter, ChannelsCommentAdapter):
            client = adapter._client()
            try:
                if action == ACTION_REPLY:
                    return adapter.reply(client, comment, str(text).strip())
                if action == ACTION_LIKE:
                    return adapter.like(client, comment, True)
                if action == ACTION_UNLIKE:
                    return adapter.like(client, comment, False)
                if action == ACTION_DELETE:
                    return adapter.delete(client, comment)
                if action == ACTION_PIN:
                    return adapter.pin(client, comment, True)
                return adapter.pin(client, comment, False)
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        try:
            return adapter._call(action, comment, str(text or "").strip())
        except CommentActionError:
            raise
        except Exception as exc:
            logger.warning("评论操作失败（{} / {}）: {}", action, platform_type, exc)
            raise CommentActionError(f"评论操作失败：{str(exc)[:200]}") from exc


# ---------------------------------------------------------------- 能力查询

def supported_actions(platform_type: int) -> list[str]:
    """某个平台支持的评论动作列表（给前端渲染按钮用）。"""
    if platform_type == 2:
        return list(COMMENT_ACTIONS)
    if platform_type in COMMENT_PAGES:
        return list(COMMENT_ACTIONS)
    return []


def platform_capabilities() -> list[dict]:
    """各平台评论能力矩阵（给前端展示"这个平台能做什么"）。"""
    result = []
    for platform_type, name in sorted(PLATFORM_NAMES.items()):
        result.append({
            "platform": platform_type,
            "name": name,
            "supported": platform_type in COMMENT_PAGES,
            "actions": supported_actions(platform_type),
            "transport": "api" if platform_type == 2 else "dom",
        })
    return result
