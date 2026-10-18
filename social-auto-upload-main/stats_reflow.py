# -*- coding: utf-8 -*-
"""发布数据回流：抓取创作者后台内容数据，并匹配到发布记录。"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from patchright.async_api import async_playwright

from conf import BASE_DIR, LOCAL_CHROME_HEADLESS
from utils.base_social_media import set_init_script

DB = Path(BASE_DIR / 'db' / 'publish_jobs.db')
MAIN_DB = Path(BASE_DIR / 'db' / 'database.db')
COOKIE_DIR = Path(BASE_DIR / 'cookiesFile')
PLATFORM_NAMES = {1: '小红书', 2: '视频号', 3: '抖音', 4: '快手', 5: '微博', 6: '百家号'}

_lock = threading.Lock()
_stats_started = False
_state = {
    'running': False,
    'started_at': None,
    'finished_at': None,
    'last_result': None,
    'error': None,
}


def get_conn():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


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


def _account_rows():
    if not MAIN_DB.exists():
        return []
    conn = sqlite3.connect(MAIN_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            'SELECT id,type,userName,filePath,status FROM user_info ORDER BY id'
        ).fetchall()]
    finally:
        conn.close()


def _account_cookie(account: dict | None, platform: int) -> Path | None:
    if account and account.get('filePath'):
        path = COOKIE_DIR / str(account['filePath'])
        if path.exists():
            return path
    for row in _account_rows():
        if int(row.get('type') or 0) == int(platform):
            path = COOKIE_DIR / str(row.get('filePath') or '')
            if path.exists():
                return path
    return None


def _record_accounts(record: dict) -> list[dict | None]:
    entries = []
    for raw in str(record.get('accounts') or '').split(','):
        raw = raw.strip()
        if raw:
            entries.append(raw)
    rows = _account_rows()
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
    if not result:
        result = [r for r in rows if int(r.get('type') or 0) == int(record.get('type') or 0)]
    return result or [None]


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
        browser = await p.chromium.launch(
            headless=LOCAL_CHROME_HEADLESS, channel='chromium',
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
        )
        context = await browser.new_context(storage_state=str(cookie))
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
            await browser.close()


async def _fetch_xhs(account: dict | None, platform: int) -> list[dict]:
    from utils.log import xiaohongshu_logger
    cookie = _account_cookie(account, platform)
    if not cookie:
        return []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=LOCAL_CHROME_HEADLESS, channel='chromium')
        context = await browser.new_context(storage_state=str(cookie))
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            return await _collect_api_items(page, 'https://creator.xiaohongshu.com/new/note-manager', 10000)
        except Exception:
            xiaohongshu_logger.warning('小红书数据回流抓取失败: ' + traceback.format_exc()[-300:])
            return []
        finally:
            await context.close()
            await browser.close()


async def _fetch_tencent(account: dict | None, platform: int) -> list[dict]:
    from utils.log import tencent_logger
    cookie = _account_cookie(account, platform)
    if not cookie:
        return []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=LOCAL_CHROME_HEADLESS, channel='chromium')
        context = await browser.new_context(storage_state=str(cookie))
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            return await _collect_api_items(page, 'https://channels.weixin.qq.com/platform/post/list', 10000)
        except Exception:
            tencent_logger.warning('视频号数据回流抓取失败: ' + traceback.format_exc()[-300:])
            return []
        finally:
            await context.close()
            await browser.close()


async def _fetch_kuaishou(account: dict | None, platform: int) -> list[dict]:
    from utils.log import kuaishou_logger
    cookie = _account_cookie(account, platform)
    if not cookie:
        return []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=LOCAL_CHROME_HEADLESS, channel='chromium')
        context = await browser.new_context(storage_state=str(cookie))
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            return await _collect_api_items(page, 'https://cp.kuaishou.com/article/manage/video', 10000)
        except Exception:
            kuaishou_logger.warning('快手数据回流抓取失败: ' + traceback.format_exc()[-300:])
            return []
        finally:
            await context.close()
            await browser.close()


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
    if not items:
        return 0
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    account_name = str((account or {}).get('userName') or (account or {}).get('id') or '')
    with get_conn() as c:
        for item in items:
            content_key = f"{platform}:{account_name}:{item.get('id') or item.get('title')}"
            c.execute('''INSERT INTO content_stats
                (record_id, platform, account, content_key, title, plays, likes, comments,
                 collects, shares, followers, published_at, raw, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                record_id, platform, account_name, content_key, item.get('title', ''),
                item.get('plays', 0), item.get('likes', 0), item.get('comments', 0),
                item.get('collects', 0), item.get('shares', 0), item.get('followers', 0),
                item.get('published_at', ''), item.get('raw', ''), now,
            ))
    return len(items)


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
        sources = {}
        for account in _account_rows():
            platform = int(account.get('type') or 0)
            cookie = COOKIE_DIR / str(account.get('filePath') or '')
            if platform in FETCHERS and cookie.exists():
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
            platform_result = result['platforms'][str(platform)]
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
            platform_result = result['platforms'][str(platform)]
            platform_result['saved'] += saved
            platform_result['history'] += saved

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
        while True:
            try:
                run_stats_reflow_once()
            except Exception:
                traceback.print_exc()
            time.sleep(max(1, interval_hours) * 3600)

    threading.Thread(target=_loop, daemon=True).start()


def stats_status() -> dict:
    with _lock:
        return dict(_state)


def stats_summary() -> dict:
    """返回发布记录、每条记录的最新内容数据、平台/账号汇总。"""
    _db_prepare()
    with get_conn() as c:
        records = [dict(r) for r in c.execute(
            'SELECT * FROM publish_records ORDER BY id DESC LIMIT 100'
        ).fetchall()]
        # 按内容唯一键只取最新快照，历史快照仍保留在表中用于趋势分析。
        rows = [dict(r) for r in c.execute('''
            SELECT s.* FROM content_stats s
            INNER JOIN (
                SELECT content_key, MAX(id) AS max_id
                FROM content_stats
                WHERE content_key IS NOT NULL AND content_key != ''
                GROUP BY content_key
            ) latest ON latest.max_id = s.id
            ORDER BY s.id DESC
        ''').fetchall()]

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
            for account in _record_accounts(record)
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
    return {
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
