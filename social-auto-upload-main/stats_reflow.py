# -*- coding: utf-8 -*-
"""发布数据回流：抓取创作者后台内容数据，并匹配到发布记录。"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from loguru import logger
from patchright.async_api import async_playwright

import runtime_guard
from conf import BASE_DIR, LOCAL_CHROME_HEADLESS
from utils.base_social_media import set_init_script
from utils.account_browser import open_persistent_account_context
from utils.credential_crypto import credential_exists, resolve_credential_path
from db_utils import connect, record_schema_version

DB = Path(BASE_DIR / 'db' / 'publish_jobs.db')
MAIN_DB = Path(BASE_DIR / 'db' / 'database.db')
COOKIE_DIR = Path(BASE_DIR / 'cookiesFile')
PLATFORM_NAMES = {1: '小红书', 2: '视频号', 3: '抖音', 4: '快手', 5: '微博', 6: '百家号'}

_lock = threading.Lock()
_STATS_SUMMARY_CACHE = {}
_STATS_SUMMARY_CACHE_TTL = 20.0
_stats_started = False
_state = {
    'running': False,
    'started_at': None,
    'finished_at': None,
    'last_result': None,
    'error': None,
}


def get_conn():
    return connect(DB, timeout=30)


def _db_prepare():
    with get_conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS content_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id INTEGER,
            platform INTEGER NOT NULL,
            account TEXT DEFAULT '',
            content_key TEXT,
            title TEXT DEFAULT '',
            plays INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            collects INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            followers INTEGER DEFAULT 0,
            published_at TEXT DEFAULT '',
            raw TEXT DEFAULT '',
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        columns = {r['name'] for r in c.execute('PRAGMA table_info(content_stats)').fetchall()}
        for name, definition in {
            'content_key': 'TEXT',
            'published_at': "TEXT DEFAULT ''",
            'followers': 'INTEGER DEFAULT 0',
        }.items():
            if name not in columns:
                c.execute(f'ALTER TABLE content_stats ADD COLUMN {name} {definition}')
        c.execute('CREATE INDEX IF NOT EXISTS idx_content_stats_record ON content_stats(record_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_content_stats_key ON content_stats(content_key)')
        # content_key 唯一化：同一内容每次回流只保留一行最新快照。
        # 先清理历史重复行（保留 id 最大的一行），再建唯一索引。
        _dedupe_content_stats(c)
        c.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_content_stats_key_unique '
            f'ON content_stats(content_key) WHERE {CONTENT_KEY_PRESENT}'
        )
        record_schema_version(c, "stats_reflow", 1)


# content_key 有效的条件。它同时用在唯一索引、部分索引的 UPSERT
# 冲突目标和去重 SQL 里，必须三处完全一致，SQLite 才能匹配上索引。
CONTENT_KEY_PRESENT = "content_key IS NOT NULL AND content_key != ''"


def _dedupe_content_stats(c):
    """删除 content_key 相同的历史重复行，只保留最新的一条。

    旧版 ``_save_stats`` 是无条件 INSERT，每次回流都会给同一内容加一行。
    这里在建立唯一索引之前先把重复行清掉，否则唯一索引建不上。
    """
    try:
        c.execute(f'''DELETE FROM content_stats WHERE id NOT IN (
            SELECT MAX(id) FROM content_stats
            WHERE {CONTENT_KEY_PRESENT}
            GROUP BY content_key
        ) AND {CONTENT_KEY_PRESENT}''')
    except Exception as exc:
        # 去重失败不应该阻塞启动，索引会在下次迁移时补上；
        # 但必须留日志——建不上唯一索引会让 UPSERT 静默退化成"每次插新行"
        logger.warning('content_stats 去重失败，唯一索引可能未建立: {}', exc)


def _account_rows():
    if not MAIN_DB.exists():
        return []
    conn = connect(MAIN_DB)
    try:
        return [dict(r) for r in conn.execute(
            'SELECT id,type,userName,filePath,status FROM user_info ORDER BY id'
        ).fetchall()]
    finally:
        conn.close()


def _account_cookie(account: dict | None, platform: int) -> Path | None:
    if account and account.get('filePath'):
        path = resolve_credential_path(COOKIE_DIR / str(account['filePath']))
        if path:
            return path
    for row in _account_rows():
        if int(row.get('type') or 0) == int(platform):
            path = resolve_credential_path(COOKIE_DIR / str(row.get('filePath') or ''))
            if path:
                return path
    return None


def _account_identifiers(account: dict) -> set[str]:
    """Return every persisted name that can identify an account."""
    file_path = str(account.get('filePath') or '')
    return {
        str(value).strip()
        for value in (
            account.get('id'),
            account.get('userName'),
            file_path,
            Path(file_path).name,
        )
        if str(value or '').strip()
    }


def _is_alive_account(account: dict | None) -> bool:
    """失效账号是否视为有效。

    只有 ``status == 0（异常/登录失效）`` 才算失效，需要全局隐藏。
    ``status`` 缺失（老测试数据）或 ``-1（验证中）`` 都视为有效，
    避免校验抖动时数据来回跳。
    """
    if not account:
        return False
    if account.get('status') is None:
        return True
    try:
        return int(account.get('status')) != 0
    except (TypeError, ValueError):
        return True


def _current_account_keys(account_rows: list[dict] | None = None) -> set[tuple[int, str]]:
    rows = _account_rows() if account_rows is None else account_rows
    return {
        (int(row.get('type') or 0), identifier)
        for row in rows
        if _is_alive_account(row)
        for identifier in _account_identifiers(row)
    }


def filter_current_account_rows(
    rows: list[dict], account_rows: list[dict] | None = None
) -> list[dict]:
    """Keep statistics owned by accounts that still exist in account management."""
    keys = _current_account_keys(account_rows)
    return [
        row for row in rows
        if (int(row.get('platform') or 0), str(row.get('account') or '').strip()) in keys
    ]


def _record_accounts(
    record: dict, account_rows: list[dict] | None = None
) -> list[dict]:
    entries = []
    for raw in str(record.get('accounts') or '').split(','):
        raw = raw.strip()
        if raw:
            entries.append(raw)
    rows = _account_rows() if account_rows is None else account_rows
    # 失效账号全局隐藏：只认存活账号，已失效的不再匹配。
    rows = [r for r in rows if _is_alive_account(r)]
    record_platform = int(record.get('type') or 0)
    if record_platform:
        rows = [row for row in rows if int(row.get('type') or 0) == record_platform]
    by_id = {int(r['id']): r for r in rows}
    by_path = {str(r.get('filePath') or ''): r for r in rows}
    by_name = {Path(str(r.get('filePath') or '')).name: r for r in rows}
    result = []
    for entry in entries:
        account = by_id.get(int(entry)) if entry.isdigit() else None
        if not account:
            account = by_path.get(entry) or by_name.get(Path(entry).name)
        if account and account not in result:
            result.append(account)
    # An explicit cookie that no longer matches an account is an orphaned
    # record. It must never fall back to whichever account now occupies the
    # same platform slot.
    if not entries:
        result = list(rows)
    return result


def filter_current_account_records(
    records: list[dict], account_rows: list[dict] | None = None
) -> list[dict]:
    """Hide publish audit rows whose recorded accounts no longer exist."""
    rows = _account_rows() if account_rows is None else account_rows
    return [record for record in records if _record_accounts(record, rows)]


def filter_current_account_jobs(
    jobs: list[dict], account_rows: list[dict] | None = None
) -> list[dict]:
    """Hide queued jobs and schedule entries owned by deleted accounts."""
    normalized = []
    for job in jobs:
        item = dict(job)
        raw_accounts = item.get("accounts")
        if not raw_accounts and item.get("account_list"):
            raw_accounts = item.get("account_list")
        if not raw_accounts and item.get("account"):
            raw_accounts = item.get("account")
        if isinstance(raw_accounts, (list, tuple, set)):
            raw_accounts = ",".join(str(value) for value in raw_accounts)
        elif isinstance(raw_accounts, str) and raw_accounts.startswith("["):
            try:
                parsed = json.loads(raw_accounts)
                if isinstance(parsed, list):
                    raw_accounts = ",".join(str(value) for value in parsed)
            except (TypeError, ValueError):
                pass
        item["accounts"] = str(raw_accounts or "")
        normalized.append(item)
    return filter_current_account_records(normalized, account_rows)


def clear_account_stats(platform: int, account_name: str) -> int:
    """Remove derived statistics when an account is deleted."""
    account_name = str(account_name or '').strip()
    if not platform or not account_name:
        return 0
    _db_prepare()
    with get_conn() as c:
        cursor = c.execute(
            'DELETE FROM content_stats WHERE platform=? AND account=?',
            (int(platform), account_name),
        )
        return max(0, int(cursor.rowcount or 0))


def _to_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return int(value or 0)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(',', '').replace('+', '')
    if not text or text in {'-', '--', '暂无'}:
        return 0
    multiplier = 1
    if text.endswith('万'):
        multiplier, text = 10000, text[:-1]
    elif text.endswith('亿'):
        multiplier, text = 100000000, text[:-1]
    text = re.sub(r'[^0-9.]', '', text)
    try:
        return int(float(text or 0) * multiplier)
    except Exception:
        return 0


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in (
        '%Y年%m月%d日 %H:%M', '%Y年%m月%d日',
        '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
        '%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M', '%Y/%m/%d',
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _normalize_title(value: Any) -> str:
    text = str(value or '').lower()
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', text)[:160]


def _title_score(record_title: str, content_title: str) -> float:
    left = _normalize_title(record_title)
    right = _normalize_title(content_title)
    if not left or not right:
        return 0.0
    ratio = SequenceMatcher(None, left, right).ratio()
    if left in right or right in left:
        ratio = max(ratio, min(len(left), len(right)) / max(len(left), len(right)))
    return ratio


def _item_from_dict(item: dict) -> dict | None:
    """把不同平台 API 的字段名归一成一套指标。"""
    title = (
        item.get('title') or item.get('display_title') or item.get('desc')
        or item.get('description') or item.get('caption')
    )
    if not title:
        return None
    interact = item.get('interact') if isinstance(item.get('interact'), dict) else {}
    stats = item.get('statistics') if isinstance(item.get('statistics'), dict) else {}
    metrics = item.get('metrics') if isinstance(item.get('metrics'), dict) else {}
    extra = item.get('extra') if isinstance(item.get('extra'), dict) else {}

    def pick(*keys):
        for key in keys:
            for source in (interact, stats, metrics, extra, item):
                if key in source and source[key] is not None:
                    return source[key]
        return 0

    plays = pick('play_count', 'playCount', 'view_count', 'viewCount', 'read_count', 'watch_count', 'exposure_count')
    likes = pick('like_count', 'likeCount', 'liked_count', 'likedCount', 'digg_count')
    comments = pick('comment_count', 'commentCount', 'comments_count')
    collects = pick('collect_count', 'collectCount', 'collected_count', 'fav_count')
    shares = pick('share_count', 'shareCount', 'forward_count')
    followers = pick('follow_count', 'fans_count', 'new_fans', 'follower_count')
    if not any(_to_int(v) for v in (plays, likes, comments, collects, shares)):
        return None
    published = (
        item.get('publish_time') or item.get('create_time') or item.get('created_at')
        or item.get('post_time') or item.get('time')
    )
    if isinstance(published, (int, float)) and published > 1000000000:
        try:
            published = datetime.fromtimestamp(published).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            published = ''
    item_id = (
        item.get('id') or item.get('note_id') or item.get('aweme_id')
        or item.get('object_id') or item.get('item_id') or title
    )
    return {
        'id': str(item_id),
        'title': str(title)[:200],
        'plays': _to_int(plays),
        'likes': _to_int(likes),
        'comments': _to_int(comments),
        'collects': _to_int(collects),
        'shares': _to_int(shares),
        'followers': _to_int(followers),
        'published_at': str(published or ''),
    }


def _walk_json(value: Any, output: list[dict], depth: int = 0):
    if depth > 8:
        return
    if isinstance(value, dict):
        item = _item_from_dict(value)
        if item:
            output.append(item)
        for child in value.values():
            if isinstance(child, (dict, list)):
                _walk_json(child, output, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _walk_json(child, output, depth + 1)


def _dedupe_items(items: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for item in items:
        key = (item.get('id'), item.get('title'), item.get('published_at'))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


async def _collect_dom_items(page) -> list[dict]:
    """API 结构变化时的兜底：从内容列表卡片上的可见文字读取指标。"""
    blocks = await page.evaluate('''() => {
        const labels = ['播放', '观看', '点赞', '评论', '收藏', '分享', '涨粉'];
        const nodes = [...document.querySelectorAll('li,article,[class*=card],[class*=item],[class*=row],[class*=content]')];
        const candidates = nodes.filter(node => {
            const text = node.innerText || '';
            const count = labels.filter(label => text.includes(label)).length;
            return count >= 2 && text.length > 8 && text.length < 2500;
        });
        const deepest = candidates.filter(node => !candidates.some(other =>
            other !== node && node.contains(other) && other.innerText.trim() !== node.innerText.trim()
        ));
        const seen = new Set();
        return deepest.map(node => node.innerText.trim()).filter(text => {
            if (seen.has(text)) return false;
            seen.add(text);
            return true;
        }).slice(0, 80);
    }''')
    result = []
    date_pattern = re.compile(r'20\d{2}[年/-]\d{1,2}[月/-]\d{1,2}')
    ignored = {'播放', '观看', '点赞', '评论', '收藏', '分享', '涨粉', '已发布', '审核中', '未通过'}
    for text in blocks:
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        if not lines:
            continue
        metrics = {}
        for index, line in enumerate(lines):
            label = next((label for label in ignored if label in line), None)
            if not label:
                continue
            value = line.replace(label, '', 1).strip()
            if not value and index + 1 < len(lines):
                value = lines[index + 1]
            if re.search(r'\d', value):
                metrics[label] = value
        if len(metrics) < 2:
            continue
        published_at = ''
        for line in lines:
            match = date_pattern.search(line)
            if match:
                published_at = match.group(0)
                break
        title = ''
        for line in lines:
            if len(line) < 3 or line in ignored or date_pattern.search(line):
                continue
            if any(label in line for label in ignored) or re.fullmatch(r'[\d.%+\-色现赞万]+', line):
                continue
            title = line
            break
        if not title:
            continue
        result.append({
            'id': f'{title}|{published_at}',
            'title': title[:200],
            'plays': _to_int(metrics.get('播放') or metrics.get('观看')),
            'likes': _to_int(metrics.get('点赞')),
            'comments': _to_int(metrics.get('评论')),
            'collects': _to_int(metrics.get('收藏')),
            'shares': _to_int(metrics.get('分享')),
            'followers': _to_int(metrics.get('涨粉')),
            'published_at': published_at,
        })
    return _dedupe_items(result)


async def _collect_api_items(page, url: str, wait_ms: int = 10000) -> list[dict]:
    responses = []

    def on_response(response):
        ctype = (response.headers or {}).get('content-type', '')
        if 'json' not in ctype:
            return
        lowered = response.url.lower()
        if not any(k in lowered for k in ('content', 'note', 'post', 'video', 'manage', 'list', 'aweme')):
            return
        responses.append(response)

    page.on('response', on_response)
    await page.goto(url, wait_until='domcontentloaded', timeout=90000)
    await page.wait_for_timeout(wait_ms)
    items = []
    for response in responses[-40:]:
        try:
            data = await response.json()
            _walk_json(data, items)
        except Exception:
            continue
    items = _dedupe_items(items)
    if not items:
        items = await _collect_dom_items(page)
    return items


async def _fetch_douyin(account: dict | None, platform: int) -> list[dict]:
    from utils.log import douyin_logger
    cookie = _account_cookie(account, platform)
    if not cookie:
        return []
    async with async_playwright() as p:
        context = await open_persistent_account_context(
            p,
            cookie,
            account_id=(account or {}).get('id'),
            headless=LOCAL_CHROME_HEADLESS,
            channel='chromium',
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
        )
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            last_error = None
            for attempt in range(2):
                try:
                    await page.goto(
                        'https://creator.douyin.com/creator-micro/content/manage',
                        wait_until='domcontentloaded', timeout=90000,
                    )
                    try:
                        await page.wait_for_selector(
                            '[class^=video-card-content-]',
                            state='attached',
                            timeout=25000,
                        )
                    except Exception:
                        pass
                    await page.wait_for_timeout(2000)
                    raw = await page.evaluate('''() => {
                        const cards = [...document.querySelectorAll('[class^=video-card-]')].filter(c =>
                            c.querySelector('[class^=metric-container-]') && c.querySelector('[class^=info-time-]'));
                        return cards.map(card => {
                            const metrics = {};
                            for (const m of card.querySelectorAll('[class^=metric-item-]')) {
                                const label = m.querySelector('[class^=metric-label-]')?.textContent?.trim();
                                const value = m.querySelector('[class^=metric-value-]')?.textContent?.trim();
                                if (label && value && !(label in metrics)) metrics[label] = value;
                            }
                            return {
                                title: card.querySelector('[class^=info-title-text-]')?.textContent?.trim() || '',
                                time: card.querySelector('[class^=info-time-]')?.textContent?.trim() || '',
                                status: card.querySelector('[class^=info-status-]')?.textContent?.trim() || '',
                                metrics,
                            };
                        });
                    }''')
                    items = []
                    for row in raw:
                        metrics = row.get('metrics') or {}
                        title = row.get('title') or ''
                        if not title:
                            continue
                        items.append({
                            'id': f"{title}|{row.get('time', '')}",
                            'title': title,
                            'plays': _to_int(metrics.get('播放')),
                            'likes': _to_int(metrics.get('点赞')),
                            'comments': _to_int(metrics.get('评论')),
                            'collects': _to_int(metrics.get('收藏')),
                            'shares': _to_int(metrics.get('分享')),
                            'followers': _to_int(metrics.get('吸粉量')),
                            'published_at': row.get('time') or '',
                            'status': row.get('status') or '',
                            'raw': json.dumps(metrics, ensure_ascii=False),
                        })
                    items = _dedupe_items(items)
                    if items:
                        return items
                    last_error = RuntimeError('no content cards rendered')
                except Exception as exc:
                    last_error = exc
                if attempt == 0:
                    await page.wait_for_timeout(2000)
            douyin_logger.warning('抖音数据回流抓取失败: ' + str(last_error)[:300])
            return []
        finally:
            await context.close()


async def _fetch_xhs(account: dict | None, platform: int) -> list[dict]:
    from utils.log import xiaohongshu_logger
    cookie = _account_cookie(account, platform)
    if not cookie:
        return []
    async with async_playwright() as p:
        context = await open_persistent_account_context(
            p,
            cookie,
            account_id=(account or {}).get('id'),
            headless=LOCAL_CHROME_HEADLESS,
            channel='chromium',
        )
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            return await _collect_api_items(page, 'https://creator.xiaohongshu.com/new/note-manager', 10000)
        except Exception:
            xiaohongshu_logger.warning('小红书数据回流抓取失败: ' + traceback.format_exc()[-300:])
            return []
        finally:
            await context.close()


async def _fetch_tencent(account: dict | None, platform: int) -> list[dict]:
    from utils.log import tencent_logger
    cookie = _account_cookie(account, platform)
    if not cookie:
        return []
    async with async_playwright() as p:
        context = await open_persistent_account_context(
            p,
            cookie,
            account_id=(account or {}).get('id'),
            headless=LOCAL_CHROME_HEADLESS,
            channel='chromium',
        )
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            return await _collect_api_items(page, 'https://channels.weixin.qq.com/platform/post/list', 10000)
        except Exception:
            tencent_logger.warning('视频号数据回流抓取失败: ' + traceback.format_exc()[-300:])
            return []
        finally:
            await context.close()


async def _fetch_kuaishou(account: dict | None, platform: int) -> list[dict]:
    from utils.log import kuaishou_logger
    cookie = _account_cookie(account, platform)
    if not cookie:
        return []
    async with async_playwright() as p:
        context = await open_persistent_account_context(
            p,
            cookie,
            account_id=(account or {}).get('id'),
            headless=LOCAL_CHROME_HEADLESS,
            channel='chromium',
        )
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            return await _collect_api_items(page, 'https://cp.kuaishou.com/article/manage/video', 10000)
        except Exception:
            kuaishou_logger.warning('快手数据回流抓取失败: ' + traceback.format_exc()[-300:])
            return []
        finally:
            await context.close()


FETCHERS = {1: _fetch_xhs, 2: _fetch_tencent, 3: _fetch_douyin, 4: _fetch_kuaishou}


def _match_items(record: dict, items: list[dict], used_keys: set[str]) -> list[dict]:
    record_time = _parse_time(record.get('finished_at') or record.get('created_at'))
    file_count = max(1, len([x for x in str(record.get('files') or '').split(',') if x.strip()]))
    ranked = []
    for item in items:
        key = str(item.get('id') or f"{item.get('title')}|{item.get('published_at')}")
        if key in used_keys:
            continue
        title_score = _title_score(record.get('title') or '', item.get('title') or '')
        item_time = _parse_time(item.get('published_at'))
        hours = abs((item_time - record_time).total_seconds()) / 3600 if item_time and record_time else 24 * 30
        ranked.append((title_score, -hours, item, key))
    ranked.sort(reverse=True, key=lambda x: (x[0], x[1]))
    if not ranked or ranked[0][0] < 0.46:
        return []
    best_score = ranked[0][0]
    matched = []
    for score, _neg_hours, item, key in ranked:
        if len(matched) >= file_count or score < max(0.46, best_score - 0.12):
            break
        used_keys.add(key)
        matched.append(item)
    return matched


def _save_stats(
    platform: int,
    account: dict | None,
    items: list[dict],
    record_id: int | None = None,
):
    """把抓取到的内容数据写入 content_stats。

    历史实现是无条件 INSERT。每回流一次，同一条内容就多一行快照——
    表会无限膨胀，而所有查询（schedule_ops / ops_center / stats_reflow）
    都要靠 ``MAX(id) GROUP BY content_key`` 子查询去重，既慢又容易漏。

    现在改成 UPSERT：``content_key`` 相同就更新那一行，不再插入新行。
    这样 content_stats 里每条内容只有一行"最新快照"，
    查询可以直接按 content_key 取，不用再套子查询去重。

    为了兼容已有的历史数据，``_db_prepare`` 会先把重复行清理掉
    （保留 id 最大的一行），再建立唯一索引。
    """
    if not items:
        return 0
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    account_name = str((account or {}).get('userName') or (account or {}).get('id') or '')
    saved = 0
    with get_conn() as c:
        for item in items:
            content_key = f"{platform}:{account_name}:{item.get('id') or item.get('title')}"
            c.execute('''INSERT INTO content_stats
                (record_id, platform, account, content_key, title, plays, likes, comments,
                 collects, shares, followers, published_at, raw, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(content_key) WHERE content_key IS NOT NULL AND content_key != ''
                DO UPDATE SET
                    record_id=COALESCE(excluded.record_id, content_stats.record_id),
                    title=excluded.title,
                    plays=excluded.plays,
                    likes=excluded.likes,
                    comments=excluded.comments,
                    collects=excluded.collects,
                    shares=excluded.shares,
                    followers=excluded.followers,
                    published_at=excluded.published_at,
                    raw=excluded.raw,
                    fetched_at=excluded.fetched_at''', (
                record_id, platform, account_name, content_key, item.get('title', ''),
                item.get('plays', 0), item.get('likes', 0), item.get('comments', 0),
                item.get('collects', 0), item.get('shares', 0), item.get('followers', 0),
                item.get('published_at', ''), item.get('raw', ''), now,
            ))
            saved += 1
    return saved


def _set_state(**kwargs):
    with _lock:
        _state.update(kwargs)


def run_stats_reflow_once() -> dict:
    _db_prepare()
    _set_state(running=True, started_at=time.strftime('%Y-%m-%d %H:%M:%S'), error=None)
    result = {'platforms': {}, 'saved': 0, 'matched': 0, 'history': 0, 'unmatched': 0}
    try:
        with get_conn() as c:
            records = [dict(r) for r in c.execute(
                "SELECT * FROM publish_records WHERE status='success' ORDER BY id DESC LIMIT 200"
            ).fetchall()]

        # 即使还没有发布记录，也先回流账号下的历史作品，让看板立即可用。
        # 失效账号不再回流：Cookie 已失效，抓到的数据也是错的，还会污染看板。
        sources = {}
        for account in _account_rows():
            if not _is_alive_account(account):
                continue
            platform = int(account.get('type') or 0)
            cookie = COOKIE_DIR / str(account.get('filePath') or '')
            if platform in FETCHERS and credential_exists(cookie):
                sources[(platform, int(account['id']))] = account

        record_sources = []
        for record in reversed(records):
            platform = int(record.get('type') or 0)
            if platform not in FETCHERS:
                continue
            for account in _record_accounts(record):
                source_key = (platform, int((account or {}).get('id') or 0))
                if account:
                    sources[source_key] = account
                record_sources.append((record, source_key, account))

        cache = {}
        for source_key, account in sources.items():
            platform = source_key[0]
            try:
                cache[source_key] = asyncio.run(FETCHERS[platform](account, platform))
            except Exception:
                traceback.print_exc()
                cache[source_key] = []
            platform_result = result['platforms'].setdefault(str(platform), {
                'name': PLATFORM_NAMES.get(platform, str(platform)),
                'fetched': 0, 'saved': 0, 'matched': 0, 'history': 0,
            })
            platform_result['fetched'] += len(cache[source_key])

        used_by_source = {}
        for record, source_key, account in record_sources:
            platform = int(record.get('type') or 0)
            items = cache.get(source_key, [])
            used = used_by_source.setdefault(source_key, set())
            matched = _match_items(record, items, used)
            saved = _save_stats(platform, account, matched, int(record['id']))
            result['saved'] += saved
            result['matched'] += saved
            if not saved:
                result['unmatched'] += 1
            platform_result = result['platforms'].setdefault(str(platform), {
                'name': PLATFORM_NAMES.get(platform, str(platform)),
                'fetched': 0, 'saved': 0, 'matched': 0, 'history': 0,
            })
            platform_result['saved'] += saved
            platform_result['matched'] += saved

        # 没有关联到发布记录的历史作品也保留快照，供看板分析。
        for source_key, items in cache.items():
            platform = source_key[0]
            used = used_by_source.setdefault(source_key, set())
            remaining = [
                item for item in items
                if str(item.get('id') or f"{item.get('title')}|{item.get('published_at')}") not in used
            ]
            saved = _save_stats(platform, sources.get(source_key), remaining)
            result['saved'] += saved
            result['history'] += saved
            platform_result = result['platforms'].setdefault(str(platform), {
                'name': PLATFORM_NAMES.get(platform, str(platform)),
                'fetched': 0, 'saved': 0, 'matched': 0, 'history': 0,
            })
            platform_result['saved'] += saved
            platform_result['history'] += saved

        # 每天留一份累计快照，周报才能算"本期增量"而不是终身累计。
        try:
            import daily_metrics

            result['snapshots'] = daily_metrics.snapshot()
        except Exception as exc:  # 快照失败不影响回流主流程
            logger.warning("写每日指标快照失败: {}", exc)
            result['snapshots'] = 0

        result['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        _set_state(running=False, finished_at=result['finished_at'], last_result=result, error=None)
        return result
    except Exception as exc:
        _set_state(running=False, finished_at=time.strftime('%Y-%m-%d %H:%M:%S'), error=str(exc))
        raise


def start_stats_reflow() -> bool:
    with _lock:
        if _state['running']:
            return False
        _state['running'] = True

    def _runner():
        try:
            run_stats_reflow_once()
        except Exception:
            traceback.print_exc()

    threading.Thread(target=_runner, daemon=True).start()
    return True


def start_stats_loop(interval_hours: int = 24):
    global _stats_started
    with _lock:
        if _stats_started:
            return
        _stats_started = True

    def _loop():
        runtime_guard.beat('stats-reflow')
        _generation = runtime_guard.current_generation('stats-reflow')
        while True:
            if not runtime_guard.is_current('stats-reflow', _generation):
                return
            try:
                run_stats_reflow_once()
                runtime_guard.beat('stats-reflow')
            except Exception as exc:
                runtime_guard.beat('stats-reflow', ok=False, error=str(exc))
                traceback.print_exc()
            time.sleep(max(1, interval_hours) * 3600)

    threading.Thread(target=_loop, daemon=True).start()


def restart_stats_loop(interval_hours: int = 24) -> None:
    """线程死掉后重新拉起数据回流（看门狗用）。"""
    global _stats_started
    with _lock:
        _stats_started = False
    logger.warning("重新拉起数据回流线程")
    start_stats_loop(interval_hours)


def stats_status() -> dict:
    with _lock:
        return dict(_state)


# ---------------------------------------------------------------- 月度趋势

def monthly_trend(months: int = 6) -> dict:
    """按月汇总发布量、播放、互动，给出统一月度趋势。

    月度趋势：每个月一行，包含发布条数、成功条数、播放、点赞、
    评论、收藏、涨粉，以及环比增长率。
    """
    months = max(1, min(int(months or 6), 24))
    accounts = _account_rows()
    now = datetime.now()
    first_month = now.replace(day=1)
    # 回退 (months - 1) 个月。
    for _ in range(months - 1):
        first_month = (first_month - timedelta(days=1)).replace(day=1)
    since = first_month.strftime('%Y-%m-%d 00:00:00')

    with get_conn() as c:
        jobs = [
            dict(row) for row in c.execute(
                "SELECT type,status,title,created_at,finished_at FROM jobs "
                "WHERE created_at>=? ORDER BY id",
                (since,),
            ).fetchall()
        ]
        stats_rows = [
            dict(row) for row in c.execute(
                "SELECT platform,account,content_key,plays,likes,comments,collects,"
                "shares,followers,published_at,fetched_at FROM content_stats "
                "WHERE content_key IS NOT NULL AND content_key!=''"
            ).fetchall()
        ]
    jobs = filter_current_account_jobs(jobs, accounts) if accounts else jobs
    stats_rows = filter_current_account_rows(stats_rows, accounts) if accounts else stats_rows

    def _month_key(value: Any) -> str:
        parsed = _parse_time(value)
        return parsed.strftime('%Y-%m') if parsed else ''

    buckets: dict[str, dict] = {}
    for offset in range(months):
        # 从 first_month 往后推 offset 个月。
        year = first_month.year + (first_month.month - 1 + offset) // 12
        month = (first_month.month - 1 + offset) % 12 + 1
        key = f"{year:04d}-{month:02d}"
        buckets[key] = {
            'month': key,
            'jobs': 0,
            'success': 0,
            'failed': 0,
            'contents': 0,
            'plays': 0,
            'likes': 0,
            'comments': 0,
            'collects': 0,
            'shares': 0,
            'followers': 0,
        }

    for job in jobs:
        key = _month_key(job.get('created_at'))
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket['jobs'] += 1
        status = str(job.get('status') or '')
        if status in ('success', 'partial_success'):
            bucket['success'] += 1
        elif status in ('failed', 'rejected'):
            bucket['failed'] += 1

    for row in stats_rows:
        key = _month_key(row.get('published_at') or row.get('fetched_at'))
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket['contents'] += 1
        for metric in ('plays', 'likes', 'comments', 'collects', 'shares', 'followers'):
            bucket[metric] += int(row.get(metric) or 0)

    series = [buckets[key] for key in sorted(buckets.keys())]
    # 环比增长率。
    for index, bucket in enumerate(series):
        if index == 0:
            bucket['plays_growth'] = None
            bucket['jobs_growth'] = None
            continue
        previous = series[index - 1]
        for metric, growth_key in (('plays', 'plays_growth'), ('jobs', 'jobs_growth')):
            base = int(previous.get(metric) or 0)
            current = int(bucket.get(metric) or 0)
            bucket[growth_key] = (
                round((current - base) / base * 100, 1) if base else None
            )
        bucket['avg_plays'] = (
            round(bucket['plays'] / bucket['contents'], 1) if bucket['contents'] else 0
        )
    if series:
        series[0]['avg_plays'] = (
            round(series[0]['plays'] / series[0]['contents'], 1)
            if series[0]['contents'] else 0
        )
    return {'months': months, 'series': series}


def _empty_funnel_stages() -> list[dict]:
    """空漏斗的阶段骨架（私信库不可用时返回）。"""
    return [
        {'stage': 'received', 'name': '收到私信', 'count': 0, 'rate': 0.0},
        {'stage': 'high_intent', 'name': '高意向', 'count': 0, 'rate': 0.0},
        {'stage': 'replied', 'name': '已回复', 'count': 0, 'rate': 0.0},
        {'stage': 'lead', 'name': '沉淀线索', 'count': 0, 'rate': 0.0},
        {'stage': 'converted', 'name': '成交', 'count': 0, 'rate': 0.0},
    ]


def _contacts_in_window(contact_rows: list[dict], since: str) -> set[int]:
    """挑出落在时间窗口内的联系人 id。

    用 ``last_message_at``（最后一条消息时间）判断，没有就用 ``created_at``。
    两个字段都为空的老数据按"在窗口内"处理——宁可多算一个线索，
    也不能让历史线索凭空消失。
    """
    result: set[int] = set()
    for row in contact_rows or []:
        try:
            contact_id = int(row.get('id') or 0)
        except (TypeError, ValueError):
            continue
        if not contact_id:
            continue
        stamp = str(row.get('last_message_at') or row.get('created_at') or '').strip()
        if not stamp or stamp >= since:
            result.add(contact_id)
    return result


def conversion_funnel(days: int = 30) -> dict:
    """成交漏斗：从私信到线索到成交。

    阶段：

    1. 收到私信 —— 窗口内有来信的**客户数**
    2. 识别为高意向 —— 其中被标记为 high 的客户数
    3. 已回复 —— 其中收到过已发送回复的客户数
    4. 沉淀为线索 —— 其中 segment 落在线索类的客户数
    5. 成交 —— 其中 segment 为 converted 的客户数

    **两个关键约束**（之前的实现都踩了）：

    * **数据来源必须走私信库**。``messages`` / ``dm_contacts`` 都住在
      ``ops_center.db``，而本模块的 ``get_conn()`` 指向 ``publish_jobs.db``。
      之前这里直接用发布库连接去查私信表，接口必然抛
      ``OperationalError: no such table: messages``——漏斗看板完全打不开。

    * **五级必须同一个单位（客户数）且逐级收敛**。之前"收到私信 / 高意向"
      统计的是**消息条数**，"已回复 / 线索 / 成交"统计的是**客户数**，
      拿消息条数当分母算客户数，转化率会冒出 218% 这种数字，运营完全无法
      解读。现在统一按客户统计，并让后一级严格包含于前一级。
    """
    days = max(1, min(int(days or 30), 365))
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    try:
        import dm_ops

        scope_sql, scope_args = dm_ops.account_scope_sql("m.platform", "m.account")
        dm_conn = dm_ops.get_conn
    except Exception:
        scope_sql, scope_args = "", []
        dm_conn = None
    if dm_conn is None:
        # 私信模块不可用时给出空漏斗，而不是让接口 500。
        return {'days': days, 'stages': _empty_funnel_stages()}
    # 联系人侧也要按账号范围过滤：私信表用 (platform, account) 标识发信账号。
    # 单独取一份 scope SQL（列名不带 m. 别名），不能靠字符串替换。
    try:
        contact_scope_sql, contact_scope_args = dm_ops.account_scope_sql(
            "platform", "account")
    except Exception:
        contact_scope_sql, contact_scope_args = "", []
    with dm_conn() as c:
        rows = [
            dict(row) for row in c.execute(
                "SELECT m.direction,m.intent,m.status,m.delivery_status,m.contact_id,"
                "m.created_at FROM messages m WHERE m.created_at>=?" + scope_sql,
                [since, *scope_args],
            ).fetchall()
        ]
        contact_rows = [
            dict(row) for row in c.execute(
                "SELECT id,segment,stage,last_message_at,created_at FROM dm_contacts"
                " WHERE COALESCE(account_deleted,0)=0" + contact_scope_sql,
                list(contact_scope_args),
            ).fetchall()
        ]

    inbound = [row for row in rows if row.get('direction') == 'inbound']
    outbound_sent = [
        row for row in rows
        if row.get('direction') == 'outbound'
        and str(row.get('delivery_status') or '') == 'sent'
    ]

    def _contact_ids(source: list[dict]) -> set[int]:
        result: set[int] = set()
        for row in source:
            value = row.get('contact_id')
            if value in (None, ''):
                continue
            try:
                result.add(int(value))
            except (TypeError, ValueError):
                continue
        return result

    # 所有阶段都以"窗口内有来信的客户"为起点，逐级收敛。
    received = _contact_ids(inbound)
    high_intent = _contact_ids([
        row for row in inbound if str(row.get('intent') or '') == 'high'
    ])
    replied = received & _contact_ids(outbound_sent)
    # 线索/成交必须落在同一个时间窗口里：否则"最近 30 天成交 3 单"会变成
    # "历史累计成交 50 单"，漏斗的时间维度形同虚设。
    window_contact_ids = _contacts_in_window(contact_rows, since)
    lead = {
        int(row['id']) for row in contact_rows
        if int(row['id']) in window_contact_ids
        and str(row.get('segment') or '') in ('qualified', 'contacted', 'converted')
    } & received
    converted = {
        int(row['id']) for row in contact_rows
        if int(row['id']) in window_contact_ids
        and str(row.get('segment') or '') == 'converted'
    } & lead

    stages = [
        {'stage': 'received', 'name': '收到私信', 'count': len(received)},
        {'stage': 'high_intent', 'name': '高意向', 'count': len(high_intent & received)},
        {'stage': 'replied', 'name': '已回复', 'count': len(replied)},
        {'stage': 'lead', 'name': '沉淀线索', 'count': len(lead)},
        {'stage': 'converted', 'name': '成交', 'count': len(converted)},
    ]
    # 逐级转化率（相对上一阶段）。
    for index, item in enumerate(stages):
        if index == 0:
            item['rate'] = 100.0 if item['count'] else 0.0
            continue
        previous = stages[index - 1]['count']
        item['rate'] = (
            round(item['count'] / previous * 100, 1) if previous else 0.0
        )
    return {
        'days': days,
        'stages': stages,
        # 消息条数与客户数不是一回事：阶段计数按客户算，这里额外给出消息量，
        # 免得运营把"客户数变少"误读成"没人找我们了"。
        'inbound_messages': len(inbound),
        'outbound_messages': len(outbound_sent),
    }


def completion_rate(days: int = 30) -> dict:
    """完播率/完读率。

    平台不直接给完播率，这里用"播放 / 发布条数"作为近似指标，
    并明确标注是估算值，不冒充平台的真实完播率。
    """
    days = max(1, min(int(days or 30), 365))
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    accounts = _account_rows()
    with get_conn() as c:
        jobs = [
            dict(row) for row in c.execute(
                "SELECT type,status,created_at FROM jobs WHERE created_at>=?",
                (since,),
            ).fetchall()
        ]
        stats_rows = [
            dict(row) for row in c.execute(
                "SELECT platform,account,plays,published_at,fetched_at FROM content_stats "
                "WHERE content_key IS NOT NULL AND content_key!=''"
            ).fetchall()
        ]
    jobs = filter_current_account_jobs(jobs, accounts) if accounts else jobs
    stats_rows = filter_current_account_rows(stats_rows, accounts) if accounts else stats_rows
    published = sum(
        1 for job in jobs
        if str(job.get('status') or '') in ('success', 'partial_success')
    )
    plays = sum(int(row.get('plays') or 0) for row in stats_rows)
    contents = len(stats_rows)
    return {
        'days': days,
        'published': published,
        'contents': contents,
        'plays': plays,
        'avg_plays': round(plays / contents, 1) if contents else 0,
        # 估算的"每条内容播放完成度"指标：不是平台真实完播率。
        'estimated_completion': round(plays / published, 1) if published else 0,
        'estimated': True,
    }


def stats_summary() -> dict:
    """返回发布记录、每条记录的最新内容数据、平台/账号汇总。"""
    _db_prepare()
    accounts = _account_rows()
    _ck = tuple(sorted((str(a.get('type') or ''), str(a.get('userName') or '')) for a in accounts))
    _cached = _STATS_SUMMARY_CACHE.get(_ck)
    if _cached and time.monotonic() - _cached[0] < _STATS_SUMMARY_CACHE_TTL:
        return _cached[1]
    with get_conn() as c:
        records = [dict(r) for r in c.execute(
            'SELECT * FROM publish_records ORDER BY id DESC LIMIT 100'
        ).fetchall()]
        # content_key 现在有唯一索引，每条内容只有一行最新快照，
        # 不需要再套 MAX(id) GROUP BY 子查询去重。
        rows = [dict(r) for r in c.execute('''
            SELECT * FROM content_stats
            WHERE content_key IS NOT NULL AND content_key != ''
            ORDER BY id DESC
        ''').fetchall()]

    records = filter_current_account_records(records, accounts)
    rows = filter_current_account_rows(rows, accounts)

    by_record: dict[int, list[dict]] = {}
    platform_totals: dict[str, dict] = {}
    account_totals: dict[tuple[int, str], dict] = {}
    topic_totals: dict[str, dict] = {}
    for row in rows:
        topics = {
            re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', match, flags=re.IGNORECASE)
            for match in re.findall(r'#([^#\s]{1,30})', str(row.get('title') or ''))
        }
        topics = {topic for topic in topics if len(topic) >= 2}
        row['topics'] = sorted(topics)
        if row.get('record_id'):
            by_record.setdefault(int(row['record_id']), []).append(row)
        platform = str(row.get('platform') or '')
        platform_id = int(platform) if platform.isdigit() else 0
        p = platform_totals.setdefault(platform, {
            'platform': platform_id, 'name': PLATFORM_NAMES.get(platform_id, platform),
            'contents': 0, 'plays': 0, 'likes': 0, 'comments': 0,
            'collects': 0, 'shares': 0, 'followers': 0,
        })
        account_key = (int(row.get('platform') or 0), str(row.get('account') or ''))
        a = account_totals.setdefault(account_key, {
            'platform': account_key[0], 'name': PLATFORM_NAMES.get(account_key[0], str(account_key[0])),
            'account': account_key[1], 'contents': 0, 'plays': 0, 'likes': 0,
            'comments': 0, 'collects': 0, 'shares': 0, 'followers': 0,
        })
        for target in (p, a):
            target['contents'] += 1
            for metric in ('plays', 'likes', 'comments', 'collects', 'shares', 'followers'):
                target[metric] += int(row.get(metric) or 0)
        for topic in topics:
            t = topic_totals.setdefault(topic, {
                'topic': topic, 'contents': 0, 'plays': 0, 'likes': 0,
                'comments': 0, 'collects': 0, 'followers': 0,
            })
            t['contents'] += 1
            for metric in ('plays', 'likes', 'comments', 'collects', 'followers'):
                t[metric] += int(row.get(metric) or 0)

    for summary_row in list(platform_totals.values()) + list(account_totals.values()):
        summary_row['avg_plays'] = round(
            summary_row['plays'] / summary_row['contents'], 1
        ) if summary_row['contents'] else 0
    for topic_row in topic_totals.values():
        topic_row['avg_plays'] = round(
            topic_row['plays'] / topic_row['contents'], 1
        ) if topic_row['contents'] else 0

    for record in records:
        stats = by_record.get(int(record['id']), [])
        record['account_names'] = [
            str((account or {}).get('userName') or '')
            for account in _record_accounts(record, accounts)
            if (account or {}).get('userName')
        ]
        record['stats'] = stats
        record['stats_summary'] = {
            metric: sum(int(item.get(metric) or 0) for item in stats)
            for metric in ('plays', 'likes', 'comments', 'collects', 'shares', 'followers')
        }
        record['content_count'] = len(stats)

    top_contents = sorted(
        rows,
        key=lambda r: (int(r.get('plays') or 0), int(r.get('likes') or 0)),
        reverse=True,
    )[:20]
    _result = {
        'records': records,
        'platform_totals': sorted(
            platform_totals.values(),
            key=lambda item: (item['plays'], item['contents']),
            reverse=True,
        ),
        'account_totals': sorted(
            account_totals.values(),
            key=lambda item: (item['followers'], item['plays']),
            reverse=True,
        ),
        'topic_totals': sorted(
            topic_totals.values(),
            key=lambda item: (item['avg_plays'], item['plays']),
            reverse=True,
        )[:20],
        'top_contents': top_contents,
        'last_fetched_at': max((r.get('fetched_at') for r in rows if r.get('fetched_at')), default=None),
        'sync': stats_status(),
    }
    _STATS_SUMMARY_CACHE[_ck] = (time.monotonic(), _result)
    return _result
