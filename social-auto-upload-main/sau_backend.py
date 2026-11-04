import asyncio
import atexit
import os
os.environ.setdefault("FLOWOPS_BACKEND", "1")
import re
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from flask_cors import CORS
from myUtils.auth import check_cookies_concurrently
from flask import Flask, request, jsonify, Response, send_from_directory
from werkzeug.exceptions import NotFound
from werkzeug.utils import secure_filename
from publish_jobs import enqueue_job, start_patrol, run_patrol_once, latest_account_status
import verify_codes
from conf import BASE_DIR
from db_utils import connect as db_connect
from account_store import (
    DEFAULT_FAIL_THRESHOLD,
    PLATFORMS,
    STATUS_TEXT,
    get_store,
    platform_type_of,
    read_storage_state,
)
import runtime_config
from app_logging import logger, setup_request_logging
from utils.keep_awake import hold_system_awake
from utils.account_environment import (
    public_environment,
    remove_environment,
    update_proxy,
)
from interactive_login import PLATFORM_LOGIN_TARGETS, interactive_login_manager
from myUtils.login import (get_tencent_cookie, douyin_cookie_gen, get_ks_cookie,
                           xiaohongshu_cookie_gen, weibo_cookie_gen_sse, baijiahao_cookie_gen_sse)
# 平台发布统一走 myUtils.platform_registry，避免这里重复维护平台清单。

runtime_config.validate_server_config()

active_queues = {}
active_verify_queues = {}
active_verify_submissions = {}
active_verify_lock = threading.Lock()
pending_login_requests = {}
pending_login_lock = threading.Lock()
# 账号 cookie 校验并发度：每个校验都要开一个 Chromium，等 5~8 秒。
# 串行做的话 N 个账号要 N×8 秒；并发后总耗时约等于单个账号的耗时。
ACCOUNT_CHECK_CONCURRENCY = 4
# 账号校验"单飞"：页面挂载、手动刷新、多个标签页会同时打 /getValidAccounts。
# 以前三次请求 = 三批 Chromium 同时登录同一套视频号后台，微信会把并发会话
# 互相挤下线，于是同一份 Cookie 前一秒有效、后一秒失效（列表状态来回跳）。
# 现在同一时刻只允许一批校验，并在 TTL 内复用结果。
ACCOUNT_CHECK_CACHE_TTL = 30.0
ACCOUNT_CHECK_LOCK_TIMEOUT = 45.0
_ACCOUNT_CHECK_LOCK = threading.Lock()
_ACCOUNT_CHECK_CACHE: dict = {"at": 0.0, "rows": None}


def _account_check_cached():
    """取 30 秒内的校验结果；没有则返回 None。"""
    rows = _ACCOUNT_CHECK_CACHE.get("rows")
    at = float(_ACCOUNT_CHECK_CACHE.get("at") or 0.0)
    if rows is not None and (time.monotonic() - at) < ACCOUNT_CHECK_CACHE_TTL:
        return rows
    return None


def _account_check_store_cache(rows) -> None:
    _ACCOUNT_CHECK_CACHE["rows"] = rows
    _ACCOUNT_CHECK_CACHE["at"] = time.monotonic()
app = Flask(__name__)
setup_request_logging(app)
import schedule_ops
schedule_ops.register(app)
import publish_jobs
publish_jobs.backfill_scheduled_items()
publish_jobs.recover_pending_jobs()
import stats_reflow
import ai_content
import ai_pack_jobs
import notify_ops
notify_ops.register(app)
import ops_center
app.register_blueprint(ops_center.bp)
import dm_ops
app.register_blueprint(dm_ops.bp)
import douyin_dm_sync
app.register_blueprint(douyin_dm_sync.bp)
import xiaohongshu_dm_sync
app.register_blueprint(xiaohongshu_dm_sync.bp)
import channels_dm_sync
app.register_blueprint(channels_dm_sync.bp)
import compliance
compliance.register(app)
import media_ops
media_ops.register(app)
import schedule_suggest
schedule_suggest.register(app)
import material_ops
material_ops.register(app)

# 默认只允许本机前端跨域，避免任意站点在浏览器里调用本机接口
CORS(
    app,
    resources={r"/*": {"origins": runtime_config.CORS_ORIGINS}},
    allow_headers=["Content-Type", "Authorization", "X-YunCe-Token"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
)

# 限制上传文件大小（与 runtime_config.MAX_CONTENT_LENGTH、Nginx 的 200m 一致）
app.config['MAX_CONTENT_LENGTH'] = runtime_config.MAX_CONTENT_LENGTH


@app.before_request
def _guard_access_token():
    """可选访问令牌：仅在设置了 YUNCE_ACCESS_TOKEN 时生效。"""
    if runtime_config.access_allowed(
        request.method, request.path, request.headers, request.args
    ):
        return None
    return jsonify({"code": 401, "msg": "未授权：缺少或错误的访问令牌", "data": None}), 401


@app.route('/health', methods=['GET'])
def health():
    """启动探针：不返回任何业务数据，便于 start.bat 判断服务是否就绪。"""
    return jsonify({"code": 200, "msg": None, "data": {"status": "ok"}}), 200


@app.route('/auth/check', methods=['GET'])
def auth_check():
    """Authenticated lightweight probe used by the browser access gate."""
    return jsonify({"code": 200, "msg": None, "data": {"authenticated": True}}), 200


# ---------------------------------------------------------------- 登录账号槽位
#
# "登录账号选择"：先用平台定位到 type，再列出该平台已有账号，让前端在
# 「新建账号 / 覆盖已有账号」之间选。key 只认 PLATFORM_LOGIN_TARGETS 里的三个，
# 中文名与 type 都归一化到这里，避免前端出现 channels / tencent 两套 key。
PLATFORM_LABELS = {"douyin": "抖音", "xiaohongshu": "小红书", "tencent": "视频号"}


def _resolve_login_platform(value) -> str | None:
    """把 platform key / 中文名 / type 编号归一化成登录平台 key。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text in PLATFORM_LOGIN_TARGETS:
        return text
    for key, meta in PLATFORM_LOGIN_TARGETS.items():
        if str(meta.get("type")) == text:
            return key
    for key, label in PLATFORM_LABELS.items():
        if text == label:
            return key
    return None


def _platform_accounts(platform_key: str) -> list:
    """读取该登录平台已有的账号行（即可覆盖的登录槽位）。"""
    platform_type = PLATFORM_LOGIN_TARGETS[platform_key]["type"]
    rows = get_store(BASE_DIR).list(platform_type)
    accounts = []
    for item in rows:
        accounts.append({
            "id": item["id"],
            "name": item["name"] or f"账号{item['id']}",
            "status": item["status"],
            "statusText": item["statusText"],
            "cookieName": item["cookie"]["name"],
            # 新增：身份与健康度，前端可用来提示"这是同一个频道"或"上次校验失败原因"
            "identity": item["identity"]["masked"],
            "identityBound": item["identity"]["bound"],
            "lastCheckAt": item["lastCheckAt"],
            "failReason": item["failReason"],
        })
    return accounts


def _account_row(account_id: int):
    """按 id 取账号行，用于覆盖登录前的归属校验。"""
    db_path = Path(BASE_DIR / "db" / "database.db")
    if not db_path.is_file():
        return None
    with db_connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, type, userName FROM user_info WHERE id=?", (account_id,)
        ).fetchone()
    if row is None:
        return None
    return {"id": int(row[0]), "type": int(row[1]), "name": row[2] or ""}


@app.route('/accounts/slots', methods=['GET'])
def account_slots():
    """登录账号选择：返回平台元数据 + 可覆盖的账号列表 + 建议新名称。"""
    platform_key = _resolve_login_platform(
        request.args.get('platform') or request.args.get('type')
    )
    if platform_key is None:
        return jsonify({
            "code": 400,
            "msg": "platform 仅支持 douyin / xiaohongshu / tencent（或中文名、type 编号）",
            "data": None,
        }), 400
    accounts = _platform_accounts(platform_key)
    return jsonify({
        "code": 200,
        "msg": None,
        "data": {
            "platform": {
                "key": platform_key,
                "type": PLATFORM_LOGIN_TARGETS[platform_key]["type"],
                "label": PLATFORM_LABELS[platform_key],
            },
            "accounts": accounts,
            "nextName": f"{PLATFORM_LABELS[platform_key]}账号{len(accounts) + 1}",
        },
    }), 200


@app.route('/interactiveLogin/start', methods=['POST'])
def interactive_login_start():
    """Open an isolated official login window for the requested platform."""
    data = request.get_json(silent=True) or {}
    account_name = str(data.get('id') or '').strip()[:64] or '账号'
    platform = _resolve_login_platform(data.get('platform')) or str(
        data.get('platform') or 'douyin'
    ).strip().lower()
    if platform not in PLATFORM_LOGIN_TARGETS:
        return jsonify({
            "code": 400,
            "msg": "该平台暂不支持官方登录窗口（目前支持 抖音 / 小红书 / 视频号）",
            "data": None,
        }), 400
    try:
        account_id = int(data.get('account_id')) if data.get('account_id') else None
    except (TypeError, ValueError):
        return jsonify({"code": 400, "msg": "账号 ID 格式错误", "data": None}), 400
    # 补登模式（仅视频号）：用已有会话打开频道列表，不重新扫码。
    try:
        seed_account_id = int(data.get('seed_account_id')) if data.get('seed_account_id') else None
    except (TypeError, ValueError):
        return jsonify({"code": 400, "msg": "补登账号 ID 格式错误", "data": None}), 400
    seed_storage = None
    if seed_account_id is not None:
        if platform != 'tencent':
            return jsonify({
                "code": 400,
                "msg": "只有视频号支持「从现有会话补登频道」",
                "data": None,
            }), 400
        seed_row = _account_row(seed_account_id)
        if seed_row is None:
            return jsonify({
                "code": 404,
                "msg": "要补登的账号不存在，请刷新后重试",
                "data": None,
            }), 404
        if seed_row["type"] != PLATFORM_LOGIN_TARGETS["tencent"]["type"]:
            return jsonify({
                "code": 400,
                "msg": "只能用视频号账号的会话补登频道",
                "data": None,
            }), 400
        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cookie_row = conn.execute(
                "SELECT filePath FROM user_info WHERE id=?", (seed_account_id,)
            ).fetchone()
        cookie_name = str(cookie_row["filePath"] or "") if cookie_row else ""
        seed_path = Path(BASE_DIR / "cookiesFile" / cookie_name)
        if not cookie_name or not seed_path.is_file():
            return jsonify({
                "code": 400,
                "msg": "该账号的 Cookie 文件不存在，请先重新扫码登录一次",
                "data": None,
            }), 400
        seed_storage = str(seed_path)
    # 覆盖登录前先确认这个账号确实属于当前平台，否则会把抖音账号行写进小红书。
    if account_id is not None:
        row = _account_row(account_id)
        platform_type = PLATFORM_LOGIN_TARGETS[platform]["type"]
        if row is None:
            return jsonify({
                "code": 404,
                "msg": "要覆盖的账号不存在，可能已被删除，请刷新后重试",
                "data": None,
            }), 404
        if row["type"] != platform_type:
            return jsonify({
                "code": 400,
                "msg": (
                    f"账号「{row['name'] or row['id']}」不属于"
                    f"{PLATFORM_LABELS[platform]}，请选择本平台的账号或新建"
                ),
                "data": None,
            }), 400
    try:
        session_id, session = interactive_login_manager.start(
            account_name, account_id, platform=platform, seed_storage=seed_storage
        )
    except RuntimeError as exc:
        return jsonify({"code": 429, "msg": str(exc), "data": None}), 429
    return jsonify({
        "code": 200,
        "msg": "已用现有会话打开频道列表" if seed_storage else "官方登录窗口已启动",
        "data": {
            "session": session_id,
            "mode": "seed" if seed_storage else ("replace" if account_id else "create"),
            "accountId": account_id,
            "seedAccountId": seed_account_id,
            "platform": platform,
            "platformLabel": PLATFORM_LABELS.get(platform, platform),
            **session.snapshot(),
        },
    }), 200


@app.route('/interactiveLogin/status', methods=['GET'])
def interactive_login_status():
    session_id = str(request.args.get('id') or '').strip()[:64]
    session = interactive_login_manager.get(session_id)
    if session is None:
        return jsonify({"code": 404, "msg": "登录窗口不存在或已关闭", "data": None}), 404
    return jsonify({"code": 200, "msg": None, "data": session.snapshot()}), 200


@app.route('/interactiveLogin/frame', methods=['GET'])
def interactive_login_frame():
    session_id = str(request.args.get('id') or '').strip()[:64]
    session = interactive_login_manager.get(session_id)
    if session is None:
        return jsonify({"code": 404, "msg": "登录窗口不存在或已关闭", "data": None}), 404
    frame = b''
    for _ in range(20):
        frame, _version = session.current_frame()
        if frame:
            break
        time.sleep(0.1)
    if not frame:
        return Response(status=204)
    response = Response(frame, mimetype='image/jpeg')
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.route('/interactiveLogin/input', methods=['POST'])
def interactive_login_input():
    data = request.get_json(silent=True) or {}
    session_id = str(data.get('id') or '').strip()[:64]
    session = interactive_login_manager.get(session_id)
    if session is None:
        return jsonify({"code": 404, "msg": "登录窗口不存在或已关闭", "data": None}), 404
    kind = str(data.get('kind') or '').strip().lower()
    if kind not in {'click', 'mouse_down', 'mouse_move', 'mouse_up', 'text', 'key', 'scroll', 'reload', 'switch_channel'}:
        return jsonify({"code": 400, "msg": "不支持的登录窗口操作", "data": None}), 400
    try:
        session.submit(kind, data.get('payload') if isinstance(data.get('payload'), dict) else {})
    except RuntimeError as exc:
        return jsonify({"code": 429, "msg": str(exc), "data": None}), 429
    return jsonify({"code": 200, "msg": "已发送", "data": None}), 200


@app.route('/interactiveLogin/stop', methods=['POST'])
def interactive_login_stop():
    data = request.get_json(silent=True) or {}
    session_id = str(data.get('id') or '').strip()[:64]
    stopped = interactive_login_manager.stop(session_id)
    if not stopped:
        return jsonify({"code": 404, "msg": "登录窗口不存在或已关闭", "data": None}), 404
    return jsonify({"code": 200, "msg": "登录窗口已关闭", "data": None}), 200

# ---------------------------------------------------------------- 账号服务 v2
#
# 账号矩阵式账号模型（详见 docs/账号体系与登录优化.md）：
#   operator（使用者，可用 YUNCE_ACCESS_TOKEN 做本机校验）
#     └─ account（媒体号）= (平台, 平台侧身份 platform_user_id) 唯一
#          ├─ credentials     cookiesFile/<name>.json（Playwright storage_state）
#          ├─ health          status / lastCheckAt / failReason
#          └─ auth_sessions   授权审计（谁在什么时候授权了哪个账号）
#
# v1 的 /getAccounts 保持 5 列老结构以免动前端；需要结构化数据和运维动作的走这里。


def _platform_catalog() -> list:
    return [
        {"type": pid, "key": meta["key"], "label": meta["label"]}
        for pid, meta in sorted(PLATFORMS.items())
    ]


@app.route('/api/v2/accounts', methods=['GET'])
def api_v2_accounts():
    """结构化账号列表：平台元数据 + 身份 + 健康度 + 凭据状态 + 汇总。"""
    platform = request.args.get('platform') or request.args.get('type')
    store = get_store(BASE_DIR)
    pid = platform_type_of(platform) if platform else None
    return jsonify({
        "code": 200,
        "msg": None,
        "data": {
            "accounts": store.list(pid),
            "stats": store.stats(),
            "platforms": _platform_catalog(),
            "statusText": {str(key): value for key, value in STATUS_TEXT.items()},
        },
    }), 200


@app.route('/api/v2/accounts/<int:account_id>', methods=['GET'])
def api_v2_account(account_id: int):
    item = get_store(BASE_DIR).get(account_id)
    if item is None:
        return jsonify({"code": 404, "msg": "账号不存在", "data": None}), 404
    return jsonify({"code": 200, "msg": None, "data": item}), 200


@app.route('/api/v2/accounts/<int:account_id>/credentials', methods=['GET'])
def api_v2_account_credentials(account_id: int):
    """凭据脱敏预览：只回 Cookie 名称/域名与 localStorage 键名，绝不回值。"""
    store = get_store(BASE_DIR)
    item = store.get(account_id)
    if item is None:
        return jsonify({"code": 404, "msg": "账号不存在", "data": None}), 404
    state = read_storage_state(Path(BASE_DIR / "cookiesFile" / item["cookie"]["name"]))
    if state is None:
        return jsonify({"code": 404, "msg": "凭据文件不存在或无法解析", "data": None}), 404
    cookies = [item for item in state.get("cookies") or [] if isinstance(item, dict)]
    storage_keys = []
    for origin in state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for entry in origin.get("localStorage") or []:
            if isinstance(entry, dict) and entry.get("name"):
                storage_keys.append(str(entry["name"]))
    public_account = {
        key: value for key, value in item.items() if key != "identity"
    }
    public_account["identity"] = {
        "masked": item["identity"]["masked"],
        "source": item["identity"]["source"],
        "bound": item["identity"]["bound"],
    }
    return jsonify({
        "code": 200,
        "msg": None,
        "data": {
            "account": public_account,
            "cookieNames": [str(cookie.get("name") or "") for cookie in cookies],
            "domains": sorted({str(cookie.get("domain") or "") for cookie in cookies} - {""}),
            "storageKeyCount": len(storage_keys),
            "storageKeys": sorted(set(storage_keys))[:200],
        },
    }), 200


@app.route('/api/v2/accounts/<int:account_id>/check', methods=['POST'])
async def api_v2_account_check(account_id: int):
    """单账号健康检查：立刻回写 status / lastCheckAt / failReason。

    状态仍按"连续 N 次失败才判异常"收敛，所以返回里给的是**落库后的有效状态**，
    前端据此显示，避免"接口说失效、列表还是正常"的不一致。
    """
    store = get_store(BASE_DIR)
    item = store.get(account_id)
    if item is None:
        return jsonify({"code": 404, "msg": "账号不存在", "data": None}), 404
    if not item["cookie"]["exists"]:
        store.mark_status(account_id, 0, "凭据文件缺失，请重新扫码授权")
        fresh = store.get(account_id) or item
        return jsonify({
            "code": 200, "msg": None,
            "data": {
                "alive": False,
                "status": fresh["status"],
                "statusText": fresh["statusText"],
                "reason": fresh["failReason"],
            },
        }), 200
    flags = await check_cookies_concurrently(
        [(item["platform"]["type"], item["cookie"]["name"])], max_concurrent=1
    )
    alive = bool(flags.get(0, False))
    store.mark_checked(
        account_id, alive, "" if alive else "登录态失效，请重新授权"
    )
    fresh = store.get(account_id) or item
    return jsonify({
        "code": 200, "msg": None,
        "data": {
            "alive": alive,
            "status": fresh["status"],
            "statusText": fresh["statusText"],
            "reason": fresh["failReason"],
        },
    }), 200


@app.route('/api/v2/accounts/<int:account_id>/environment', methods=['GET', 'PUT'])
def api_v2_account_environment(account_id: int):
    """Read or update the account's persistent profile and proxy settings."""
    item = get_store(BASE_DIR).get(account_id)
    if item is None or not item["cookie"].get("name"):
        return jsonify({"code": 404, "msg": "账号不存在", "data": None}), 404
    cookie_path = Path(BASE_DIR / "cookiesFile" / item["cookie"]["name"])
    if request.method == 'GET':
        return jsonify({
            "code": 200, "msg": None,
            "data": public_environment(cookie_path, account_id),
        }), 200
    data = request.get_json(silent=True) or {}
    proxy = data.get("proxy") if isinstance(data.get("proxy"), dict) else {}
    try:
        environment = update_proxy(
            cookie_path,
            account_id,
            server=str(proxy.get("server") or "").strip(),
            username=str(proxy.get("username") or "").strip(),
            password=(str(proxy.get("password")) if "password" in proxy else None),
        )
    except ValueError as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "环境配置已保存", "data": environment}), 200


@app.route('/api/v2/accounts/prune-cookies', methods=['POST'])
def api_v2_prune_cookies():
    """清理没有被任何账号引用的 Cookie 文件；默认 dry-run。"""
    dry_run = str(request.args.get('dry') or '1') not in {'0', 'false', 'False'}
    result = get_store(BASE_DIR).prune_orphans(dry_run=dry_run)
    return jsonify({"code": 200, "msg": None, "data": result}), 200


@app.route('/api/v2/accounts/backfill-identities', methods=['POST'])
def api_v2_backfill_identities():
    """为历史账号补平台身份（导入旧数据、或身份识别规则升级后手动重跑）。"""
    force = str(request.args.get('force') or '').lower() in {'1', 'true', 'yes'}
    result = get_store(BASE_DIR).backfill_identities(force=force)
    return jsonify({"code": 200, "msg": None, "data": result}), 200


@app.route('/api/v2/auth-sessions', methods=['GET'])
def api_v2_auth_sessions():
    """授权会话审计：包含已结束/被中断的会话，进程重启后仍可查。"""
    limit = request.args.get('limit', type=int) or 20
    return jsonify({
        "code": 200, "msg": None,
        "data": get_store(BASE_DIR).list_auth_sessions(limit),
    }), 200


def _alias_v2_routes() -> None:
    """/v2/* 别名。

    vite 的 dev proxy 把 ``/api`` 前缀 rewrite 掉再转发（见 vite.config.js），
    因此经由 5173 访问时后端收到的是 ``/v2/accounts``。生产环境 nginx 是整段
    透传，走的是 ``/api/v2/accounts``。两边都注册一份，避免"本地能开、构建后 404"。
    """
    for rule in list(app.url_map.iter_rules()):
        if not rule.rule.startswith('/api/v2/'):
            continue
        alias = rule.rule[len('/api'):]
        if alias in {item.rule for item in app.url_map.iter_rules()}:
            continue
        app.add_url_rule(
            alias,
            endpoint=f"alias_{rule.endpoint}",
            view_func=app.view_functions[rule.endpoint],
            methods=sorted(rule.methods - {'HEAD', 'OPTIONS'}),
        )


_alias_v2_routes()


# 获取当前目录（假设 index.html 和 assets 在这里）
current_dir = os.path.dirname(os.path.abspath(__file__))

# 处理所有静态资源请求（未来打包用）
@app.route('/assets/<filename>')
def custom_static(filename):
    """返回前端打包产物。

    Vite 产出的文件名带内容哈希（``index-CV6JzdQ9.js``），内容变了文件名
    就会变，所以可以放心用一年的强缓存。之前的响应没有任何 Cache-Control，
    浏览器每次都要重新问服务器、重新下载 1MB 的 JS。

    ``index.html`` 则相反：它引用的是带哈希的资源，必须每次都回源校验，
    否则发新版后用户会一直拿到旧的 html 指向已经不存在的旧资源。
    """
    response = send_from_directory(os.path.join(current_dir, 'assets'), filename)
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response


# 处理 favicon.ico 静态资源（未来打包用）
@app.route('/favicon.ico')
def favicon():
    response = send_from_directory(os.path.join(current_dir, 'assets'), 'vite.svg')
    response.headers['Cache-Control'] = 'public, max-age=604800'
    return response

@app.route('/vite.svg')
def vite_svg():
    response = send_from_directory(os.path.join(current_dir, 'assets'), 'vite.svg')
    response.headers['Cache-Control'] = 'public, max-age=604800'
    return response

# （未来打包用）
@app.route('/')
def index():  # put application's code here
    response = send_from_directory(current_dir, 'index.html')
    # index.html 不带哈希，必须每次回源校验，否则会一直用旧版本
    response.headers['Cache-Control'] = 'no-cache, must-revalidate'
    return response

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No file part in the request"
        }), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No selected file"
        }), 400
    try:
        # 保存文件到指定位置
        uuid_v1 = uuid.uuid1()
        logger.debug("upload uuid={}", uuid_v1)
        safe_name = secure_filename(file.filename)
        if not safe_name:
            return jsonify({"code": 400, "data": None, "msg": "Invalid filename"}), 400
        filepath = Path(BASE_DIR / "videoFile" / f"{uuid_v1}_{safe_name}")
        file.save(filepath)
        return jsonify({"code":200,"msg": "File uploaded successfully", "data": f"{uuid_v1}_{safe_name}"}), 200
    except Exception as e:
        return jsonify({"code":500,"msg": str(e),"data":None}), 500

@app.route('/getFile', methods=['GET'])
def get_file():
    # 获取 filename 参数
    filename = request.args.get('filename')

    if not filename:
        return jsonify({"code": 400, "msg": "filename is required", "data": None}), 400

    # 防止路径穿越攻击
    if '..' in filename or filename.startswith('/'):
        return jsonify({"code": 400, "msg": "Invalid filename", "data": None}), 400

    # 拼接完整路径
    file_path = str(Path(BASE_DIR / "videoFile"))

    # 返回文件
    return send_from_directory(file_path,filename)


@app.route('/download/<path:file_path>', methods=['GET'])
def download_file(file_path):
    """下载素材（前端 materialApi.downloadMaterial 调用此路由）。

    与 /getFile 一样只允许访问 videoFile 目录内的文件，
    并显式拦截路径穿越，避免 ../ 读到目录外的文件。
    """
    if not file_path:
        return jsonify({"code": 400, "msg": "file path is required", "data": None}), 400

    # 防止路径穿越攻击（Windows 下也要防反斜杠）
    normalized = str(file_path).replace('\\', '/')
    if '..' in normalized or normalized.startswith('/'):
        return jsonify({"code": 400, "msg": "Invalid file path", "data": None}), 400

    try:
        return send_from_directory(
            str(Path(BASE_DIR / "videoFile")),
            normalized,
            as_attachment=True,
        )
    except NotFound:
        return jsonify({"code": 404, "msg": "File not found", "data": None}), 404


@app.route('/dm/media/<path:file_path>', methods=['GET'])
def dm_media_file(file_path):
    """Serve locally cached private-message assets inside the DM media root."""
    if not file_path:
        return jsonify({"code": 400, "msg": "file path is required", "data": None}), 400
    normalized = str(file_path).replace('\\', '/').lstrip('/')
    root = (Path(BASE_DIR) / "dm_media").resolve()
    target = (root / normalized).resolve()
    if not target.is_relative_to(root) or target == root:
        return jsonify({"code": 400, "msg": "Invalid file path", "data": None}), 400
    try:
        return send_from_directory(
            str(root), normalized,
            as_attachment=request.args.get('download') in ('1', 'true', 'yes'),
            conditional=True,
        )
    except NotFound:
        return jsonify({"code": 404, "msg": "Media not found", "data": None}), 404


@app.route('/uploadSave', methods=['POST'])
def upload_save():
    if 'file' not in request.files:
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No file part in the request"
        }), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No selected file"
        }), 400

    # 获取表单中的自定义文件名（可选）
    custom_filename = request.form.get('filename', None)
    if custom_filename:
        filename = secure_filename(custom_filename + "." + file.filename.split('.')[-1])
    else:
        filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"code": 400, "data": None, "msg": "Invalid filename"}), 400

    try:
        # 生成 UUID v1
        uuid_v1 = uuid.uuid1()
        logger.debug("chunk upload uuid={}", uuid_v1)

        # 构造文件名和路径
        final_filename = f"{uuid_v1}_{filename}"
        filepath = Path(BASE_DIR / "videoFile" / f"{uuid_v1}_{filename}")

        # 保存文件
        file.save(filepath)
        file_md5 = material_ops.file_md5(filepath)
        duplicate = material_ops.duplicate_count(file_md5) > 0

        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                                INSERT INTO file_records (filename, filesize, file_path, md5, tags)
            VALUES (?, ?, ?, ?, ?)
                                ''', (filename, round(float(os.path.getsize(filepath)) / (1024 * 1024),2), final_filename, file_md5, ''))
            conn.commit()
            logger.info("上传文件已记录")

        return jsonify({
            "code": 200,
            "msg": "File uploaded and saved successfully",
            "data": {
                "filename": filename,
                "filepath": final_filename,
                "duplicate": duplicate,
                "md5": file_md5
            }
        }), 200

    except Exception as e:
        logger.exception("Upload failed: {}", e)
        return jsonify({
            "code": 500,
            "msg": f"upload failed: {e}",
            "data": None
        }), 500

@app.route('/getFiles', methods=['GET'])
def get_all_files():
    try:
        # 使用 with 自动管理数据库连接
        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()

            # 查询所有记录
            cursor.execute("SELECT * FROM file_records")
            rows = cursor.fetchall()

            # 将结果转为字典列表，并提取UUID
            data = []
            for row in rows:
                row_dict = dict(row)
                # 从 file_path 中提取 UUID (文件名的第一部分，下划线前)
                if row_dict.get('file_path'):
                    file_path_parts = row_dict['file_path'].split('_', 1)  # 只分割第一个下划线
                    if len(file_path_parts) > 0:
                        row_dict['uuid'] = file_path_parts[0]  # UUID 部分
                    else:
                        row_dict['uuid'] = ''
                else:
                    row_dict['uuid'] = ''
                data.append(row_dict)

            md5_counts = {}
            for item in data:
                md5 = item.get('md5') or ''
                if md5:
                    md5_counts[md5] = md5_counts.get(md5, 0) + 1
            for item in data:
                item['duplicate'] = md5_counts.get(item.get('md5') or '', 0) > 1

            return jsonify({
                "code": 200,
                "msg": "success",
                "data": data
            }), 200
    except Exception:
        return jsonify({
            "code": 500,
            "msg": str("get file failed!"),
            "data": None
        }), 500


@app.route("/getAccounts", methods=['GET'])
def getAccounts():
    """快速获取所有账号信息，不进行cookie验证。

    仍返回历史 5 列结构 ``[id, type, filePath, userName, status]``，前端无需改；
    结构化字段（平台元数据、身份、健康度、凭据状态）走 ``/api/v2/accounts``。
    """
    try:
        rows_list = get_store(BASE_DIR).legacy_rows()
        logger.debug("账号列表（快速获取）：{} 条", len(rows_list))
        return jsonify(
            {
                "code": 200,
                "msg": None,
                "data": rows_list
            }), 200
    except Exception as e:
        logger.exception("获取账号列表时出错: {}", e)
        return jsonify({
            "code": 500,
            "msg": f"获取账号列表失败: {str(e)}",
            "data": None
        }), 500


@app.route("/getValidAccounts",methods=['GET'])
async def getValidAccounts():
    """校验所有账号的登录状态并回写健康度。

    这一版解决三个"状态来回跳"的根因：

    1. **并发重复校验**：页面挂载、手动刷新、多标签页会同时打这个接口。
       以前三次请求 = 三批 Chromium 同时访问平台后台，视频号同一 wxuin
       多开会被判定"环境异常"并互相挤下线，于是同一份 Cookie 前一秒有效、
       后一秒失效。现在用单飞锁 + 30 秒结果缓存：同一时刻只跑一批，
       并发请求直接拿同一份结果。
    2. **同平台并发自查**：``check_cookies_concurrently`` 里视频号已改为串行。
    3. **单次失败就拉红**：改为"连续 2 次失败才判异常"（见 ``mark_checked``）。

    另外：凭据文件缺失是确定状态，不占用失败次数，直接标异常。
    """
    store = get_store(BASE_DIR)
    cached = _account_check_cached()
    if cached is not None:
        return jsonify({"code": 200, "msg": None, "data": cached}), 200

    acquired = _ACCOUNT_CHECK_LOCK.acquire(timeout=ACCOUNT_CHECK_LOCK_TIMEOUT)
    if not acquired:
        # 另一批校验还在跑且已超时：先返回当前库里的状态，别让请求堆积
        logger.warning("账号校验排队超时，返回当前库内状态")
        return jsonify({"code": 200, "msg": None, "data": store.legacy_rows()}), 200
    try:
        cached = _account_check_cached()  # 等锁期间别人可能已经跑完
        if cached is not None:
            return jsonify({"code": 200, "msg": None, "data": cached}), 200

        accounts = store.list()
        if not accounts:
            return jsonify({"code": 200, "msg": None, "data": []}), 200

        pending = []
        for index, account in enumerate(accounts):
            if not account["cookie"]["exists"]:
                store.mark_status(
                    account["id"], 0, "凭据文件缺失，请重新扫码授权"
                )
                continue
            pending.append((index, account["platform"]["type"], account["cookie"]["name"]))

        alive_flags = await check_cookies_concurrently(
            [(platform_type, cookie_name) for _index, platform_type, cookie_name in pending],
            max_concurrent=ACCOUNT_CHECK_CONCURRENCY,
        )
        dead = 0
        for position, (index, _platform_type, _cookie_name) in enumerate(pending):
            alive = bool(alive_flags.get(position, False))
            if not alive:
                dead += 1
            store.mark_checked(
                accounts[index]["id"],
                alive,
                "" if alive else "登录态失效，请重新授权",
            )
        data = store.legacy_rows()
        _account_check_store_cache(data)
        logger.info(
            "账号校验完成：共 {} 个，其中本次未通过 {} 个（连续 {} 次失败才判异常）",
            len(accounts), dead, DEFAULT_FAIL_THRESHOLD,
        )
        return jsonify({"code": 200, "msg": None, "data": data}), 200
    finally:
        _ACCOUNT_CHECK_LOCK.release()

@app.route('/deleteFile', methods=['GET'])
def delete_file():
    file_id = request.args.get('id')

    if not file_id or not file_id.isdigit():
        return jsonify({
            "code": 400,
            "msg": "Invalid or missing file ID",
            "data": None
        }), 400

    try:
        # 获取数据库连接
        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()

            # 查询要删除的记录
            cursor.execute("SELECT * FROM file_records WHERE id = ?", (file_id,))
            record = cursor.fetchone()

            if not record:
                return jsonify({
                    "code": 404,
                    "msg": "File not found",
                    "data": None
                }), 404

            record = dict(record)

            # 获取文件路径并删除实际文件
            file_path = Path(BASE_DIR / "videoFile" / record['file_path'])
            if file_path.exists():
                try:
                    file_path.unlink()  # 删除文件
                    logger.info("实际文件已删除: {}", file_path)
                except Exception as e:
                    logger.warning("删除实际文件失败: {}", e)
                    # 即使删除文件失败，也要继续删除数据库记录，避免数据不一致
            else:
                logger.warning("实际文件不存在: {}", file_path)

            # 删除数据库记录
            cursor.execute("DELETE FROM file_records WHERE id = ?", (file_id,))
            conn.commit()

        return jsonify({
            "code": 200,
            "msg": "File deleted successfully",
            "data": {
                "id": record['id'],
                "filename": record['filename']
            }
        }), 200

    except Exception:
        return jsonify({
            "code": 500,
            "msg": str("delete failed!"),
            "data": None
        }), 500

@app.route('/deleteAccount', methods=['GET'])
def delete_account():
    account_id = request.args.get('id')

    if not account_id or not account_id.isdigit():
        return jsonify({
            "code": 400,
            "msg": "Invalid or missing account ID",
            "data": None
        }), 400

    account_id = int(account_id)

    try:
        # 获取数据库连接
        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()

            # 查询要删除的记录
            cursor.execute("SELECT * FROM user_info WHERE id = ?", (account_id,))
            record = cursor.fetchone()

            if not record:
                return jsonify({
                    "code": 200,
                    "msg": "账号不存在或已删除",
                    "data": None
                }), 200

            record = dict(record)

            # 删除关联的cookie文件
            if record.get('filePath'):
                cookie_file_path = Path(BASE_DIR / "cookiesFile" / record['filePath'])
                try:
                    remove_environment(cookie_file_path, account_id)
                except Exception as env_err:
                    logger.warning("删除账号持久浏览器目录失败: {}", env_err)
                if cookie_file_path.exists():
                    try:
                        cookie_file_path.unlink()
                        logger.info("Cookie 文件已删除: {}", cookie_file_path)
                    except Exception as e:
                        logger.warning("删除 Cookie 文件失败: {}", e)

            # 删除数据库记录
            cursor.execute("DELETE FROM user_info WHERE id = ?", (account_id,))
            conn.commit()

        # 巡检记录是账号派生台账；账号已经删除后必须同步移除，避免 Todo 和
        # 状态接口继续统计不存在的账号。历史数据从巡检表本身可追溯。
        try:
            publish_jobs.clear_account_patrol(account_id)
        except Exception as patrol_err:
            logger.warning("清理账号巡检台账失败（不影响账号删除）: {}", patrol_err)
        ops_center.invalidate_todo_cache()

        # 隐藏该账号在私信中心的会话（聊天记录保留在本地，不再展示）
        try:
            _platform_name = {
                1: "小红书", 2: "视频号", 3: "抖音",
                4: "快手", 5: "微博", 6: "百家号",
            }.get(record.get("type") if isinstance(record, dict) else None, "")
            _account_name = record.get("userName") if isinstance(record, dict) else None
            if _account_name:
                with dm_ops.get_conn() as dm_conn:
                    cols = {row["name"] for row in dm_conn.execute(
                        "PRAGMA table_info(dm_contacts)").fetchall()}
                    if "account_deleted" not in cols:
                        dm_conn.execute(
                            "ALTER TABLE dm_contacts ADD COLUMN account_deleted INTEGER DEFAULT 0")
                    if _platform_name:
                        dm_conn.execute(
                            """UPDATE dm_contacts SET account_deleted=1
                                WHERE platform=? AND account=?""",
                            (_platform_name, _account_name))
                    else:
                        dm_conn.execute(
                            "UPDATE dm_contacts SET account_deleted=1 WHERE account=?",
                            (_account_name,))
                    dm_conn.commit()
        except Exception as dm_err:
            logger.warning("清理账号私信数据失败（不影响账号删除）: {}", dm_err)

        # Derived statistics belong to the deleted account and should not
        # resurface when another account signs into the same platform.
        try:
            stats_reflow.clear_account_stats(record.get("type"), record.get("userName"))
        except Exception as stats_err:
            logger.warning("清理账号统计快照失败（不影响账号删除）: {}", stats_err)

        return jsonify({
            "code": 200,
            "msg": "account deleted successfully",
            "data": None
        }), 200

    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"delete failed: {str(e)}",
            "data": None
        }), 500


# SSE 登录接口
@app.route('/jobs', methods=['GET'])
def list_jobs():
    limit = request.args.get('limit', 50)
    with publish_jobs.get_conn() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT ?', (int(limit),)).fetchall()]
    rows = stats_reflow.filter_current_account_jobs(rows)
    return jsonify({'code': 200, 'data': rows, 'msg': None}), 200

@app.route('/jobs/<int:job_id>', methods=['GET'])
def get_job(job_id):
    with publish_jobs.get_conn() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        return jsonify({'code': 404, 'msg': 'job not found'}), 404
    return jsonify({'code': 200, 'data': dict(row), 'msg': None}), 200


def _verify_request_payload(item):
    data = dict(item or {})
    account_file = os.path.basename(str(data.get('account_file') or ''))
    if account_file:
        try:
            conn = db_connect(Path(BASE_DIR / "db" / "database.db"))
            row = conn.execute('SELECT userName, type FROM user_info WHERE filePath=?', (account_file,)).fetchone()
            conn.close()
            if row:
                data['account_name'] = row['userName']
                names = {1: '小红书', 2: '视频号', 3: '抖音', 4: '快手', 5: '微博', 6: '百家号'}
                data['platform'] = names.get(row['type'], data.get('platform') or '')
        except Exception:
            pass
    return data


@app.route('/verify/pending', methods=['GET'])
def verify_pending():
    pending = verify_codes.latest_pending()
    return jsonify({
        'code': 200,
        'data': _verify_request_payload(pending) if pending else None,
        'msg': None,
    }), 200


@app.route('/verify/submit', methods=['POST'])
def verify_submit_code():
    payload = request.get_json(silent=True) or {}
    request_id = str(payload.get('request_id') or '').strip()
    code = str(payload.get('code') or '').strip()
    if not request_id:
        return jsonify({'code': 400, 'data': None, 'msg': '缺少验证码请求标识'}), 400
    if not code.isdigit() or not (4 <= len(code) <= 8):
        return jsonify({'code': 400, 'data': None, 'msg': '请输入 4-8 位数字验证码'}), 400
    ok, msg = verify_codes.submit_code(request_id, code)
    if not ok:
        return jsonify({'code': 409, 'data': None, 'msg': msg}), 409
    return jsonify({'code': 200, 'data': {'request_id': request_id}, 'msg': '验证码已提交'}), 200


@app.route('/verify/resend', methods=['POST'])
def verify_resend_code():
    payload = request.get_json(silent=True) or {}
    request_id = str(payload.get('request_id') or '').strip()
    if not request_id:
        return jsonify({'code': 400, 'data': None, 'msg': '缺少验证码请求标识'}), 400
    ok, msg = verify_codes.request_resend(request_id)
    if not ok:
        return jsonify({'code': 409, 'data': None, 'msg': msg}), 409
    return jsonify({'code': 200, 'data': {'request_id': request_id}, 'msg': '已请求重新发送验证码'}), 200


@app.route('/review/pending', methods=['GET'])
def review_pending():
    status = request.args.get('status', 'pending_review')
    with publish_jobs.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            'SELECT * FROM jobs WHERE status=? ORDER BY id DESC', (status,)).fetchall()]
    return jsonify({'code': 200, 'data': rows, 'msg': None}), 200


@app.route('/review/<int:job_id>/approve', methods=['POST'])
def review_approve(job_id):
    publish_jobs.approve_job(job_id)
    return jsonify({'code': 200, 'msg': 'approved'}), 200


@app.route('/review/<int:job_id>/reject', methods=['POST'])
def review_reject(job_id):
    publish_jobs.reject_job(job_id)
    return jsonify({'code': 200, 'msg': 'rejected'}), 200

@app.route('/publish-records', methods=['GET'])
def list_publish_records():
    limit = request.args.get('limit', 100)
    with publish_jobs.get_conn() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM publish_records ORDER BY id DESC LIMIT ?', (int(limit),)).fetchall()]
    rows = stats_reflow.filter_current_account_records(rows)
    return jsonify({'code': 200, 'data': rows, 'msg': None}), 200


# ---------------------------------------------------------------- 发布任务（账号级）
#
# 一个发布任务在入库后会展开成 (素材 × 账号) 的子任务，每条独立记账。
# 这里暴露子任务明细、进度、取消与"只重试失败项"：
#   * 失败能定位到具体账号/素材，而不是整批一个字符串；
#   * 重试不会把已经成功的账号再发一遍（旧实现的重复发布 bug）；
#   * 可以中途取消。

@app.route('/publish/jobs/<int:job_id>', methods=['GET'])
def publish_job_detail(job_id: int):
    with publish_jobs.get_conn() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (int(job_id),)).fetchone()
    if row is None:
        return jsonify({'code': 404, 'data': None, 'msg': '任务不存在'}), 404
    return jsonify({
        'code': 200,
        'data': {
            'job': dict(row),
            'progress': publish_jobs.job_progress(job_id),
            'items': publish_jobs.job_items(job_id),
        },
        'msg': None,
    }), 200


@app.route('/publish/jobs/<int:job_id>/items', methods=['GET'])
def publish_job_items(job_id: int):
    """子任务明细 + 进度（前端"发布记录"展开行用）。"""
    return jsonify({
        'code': 200,
        'data': {
            'progress': publish_jobs.job_progress(job_id),
            'items': publish_jobs.job_items(job_id),
        },
        'msg': None,
    }), 200


@app.route('/publish/jobs/<int:job_id>/cancel', methods=['POST'])
def publish_job_cancel(job_id: int):
    result = publish_jobs.cancel_job(job_id)
    return jsonify({'code': 200, 'data': result, 'msg': '已取消'}), 200


@app.route('/publish/jobs/<int:job_id>/retry-failed', methods=['POST'])
def publish_job_retry_failed(job_id: int):
    """只重试失败的子任务，已经成功的账号不会重复发布。"""
    result = publish_jobs.retry_failed_items(job_id)
    return jsonify({
        'code': 200,
        'data': result,
        'msg': f"已重新排队 {result.get('retried', 0)} 条失败子任务",
    }), 200


@app.route(
    '/publish/jobs/<int:job_id>/items/<int:item_id>/remote-post',
    methods=['DELETE'],
)
def publish_item_delete_remote_post(job_id: int, item_id: int):
    """Delete one Channels post after an explicit UI confirmation."""
    from channels_dm import ChannelsAuthError, ChannelsError

    payload = request.get_json(silent=True) or {}
    expected_remote_id = str(payload.get('remoteId') or '').strip()
    if not expected_remote_id:
        return jsonify({
            'code': 400, 'data': None,
            'msg': '请确认要删除的平台作品 ID',
        }), 400
    try:
        result = publish_jobs.delete_remote_item(
            job_id, item_id, expected_remote_id=expected_remote_id,
        )
    except LookupError as exc:
        return jsonify({'code': 404, 'data': None, 'msg': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'code': 400, 'data': None, 'msg': str(exc)}), 400
    except ChannelsAuthError as exc:
        return jsonify({'code': 401, 'data': None, 'msg': str(exc)}), 401
    except ChannelsError as exc:
        return jsonify({'code': 502, 'data': None, 'msg': str(exc)}), 502
    return jsonify({
        'code': 200,
        'data': result,
        'msg': '平台作品已删除' if not result.get('alreadyDeleted') else '平台作品此前已删除',
    }), 200


@app.route(
    '/publish/jobs/<int:job_id>/items/<int:item_id>/replacement',
    methods=['POST'],
)
def publish_item_create_replacement(job_id: int, item_id: int):
    """Correct one item by publishing first and deleting the old post last."""
    payload = request.get_json(silent=True) or {}
    try:
        result = publish_jobs.create_replacement_job(
            job_id,
            item_id,
            title=str(payload.get('title') or '').strip(),
            tags=payload.get('tags'),
            desc=payload.get('desc'),
        )
    except LookupError as exc:
        return jsonify({'code': 404, 'data': None, 'msg': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'code': 400, 'data': None, 'msg': str(exc)}), 400
    return jsonify({
        'code': 200,
        'data': result,
        'msg': '替换任务已排队；新作品回查成功后才会删除旧作品',
    }), 200

@app.route('/dm/health', methods=['GET'])
def dm_sync_health():
    """私信同步健康度：三平台汇总 + 账号级状态。

    用来回答"为什么私信中心只有几天前的消息"：
      * 平台级：最后一次成功同步时间、错误分类、下次计划；
      * 账号级：每个账号各自最后一次成功/失败原因（最常见的是个别号登录失效）。
    前端在私信中心顶部据此显示一条可操作的横幅。
    """
    managers = (
        douyin_dm_sync.sync_manager,
        xiaohongshu_dm_sync.sync_manager,
        channels_dm_sync.sync_manager,
    )
    platforms = []
    problems = []
    for manager in managers:
        payload = manager.health_payload()
        platforms.append(payload)
        if payload["noAccounts"]:
            problems.append({
                "platform": payload["platform"],
                "kind": "no_accounts",
                "message": f"{payload['platform']}：还没有登录账号，去「账号管理」添加后即可同步",
            })
            continue
        expired = [
            item for item in payload["accounts"]
            if item.get("errorKind") == "account_expired"
        ]
        if expired:
            names = "、".join(str(item.get("name") or "") for item in expired)[:120]
            problems.append({
                "platform": payload["platform"],
                "kind": "account_expired",
                "message": (
                    f"{payload['platform']}：{len(expired)} 个账号登录失效"
                    f"（{names}），请重新扫码登录后再同步"
                ),
            })
        elif payload.get("errorKind"):
            problems.append({
                "platform": payload["platform"],
                "kind": payload["errorKind"],
                "message": f"{payload['platform']}：{str(payload.get('lastError') or '')[:160]}",
            })
    return jsonify({
        "code": 200,
        "msg": None,
        "data": {"platforms": platforms, "problems": problems},
    }), 200


@app.route('/account-status', methods=['GET'])
def account_status():
    return jsonify({'code': 200, 'data': latest_account_status(), 'msg': None}), 200

@app.route('/patrol/run', methods=['POST'])
def trigger_patrol():
    import threading as _t
    def _run():
        try:
            run_patrol_once()
        except Exception:
            import traceback
            traceback.print_exc()
    _t.Thread(target=_run, daemon=True).start()
    return jsonify({'code': 200, 'msg': 'patrol started in background'}), 200


@app.route('/stats/summary', methods=['GET'])
def stats_summary():
    return jsonify({'code': 200, 'data': stats_reflow.stats_summary(), 'msg': None}), 200


@app.route('/stats/run', methods=['POST'])
def trigger_stats_reflow():
    started = stats_reflow.start_stats_reflow()
    return jsonify({
        'code': 200,
        'data': stats_reflow.stats_status(),
        'msg': '数据同步已开始' if started else '数据同步正在进行中',
    }), 200


@app.route('/stats/status', methods=['GET'])
def stats_status():
    return jsonify({'code': 200, 'data': stats_reflow.stats_status(), 'msg': None}), 200


@app.route('/ai/status', methods=['GET'])
def ai_status():
    return jsonify({'code': 200, 'data': ai_content.config_status(), 'msg': None}), 200


@app.route('/ai/generate', methods=['POST'])
def ai_generate():
    data = request.get_json(force=True) or {}
    try:
        result = ai_content.generate_content(data)
    except ValueError as exc:
        return jsonify({'code': 400, 'data': None, 'msg': str(exc)}), 400
    except Exception as exc:
        return jsonify({'code': 500, 'data': None, 'msg': str(exc)}), 500
    return jsonify({'code': 200, 'data': result, 'msg': 'ok'}), 200


@app.route('/ai/generate-pack', methods=['POST'])
def ai_generate_pack():
    data = request.get_json(force=True) or {}
    try:
        result = ai_content.generate_pack(data)
    except ValueError as exc:
        return jsonify({'code': 400, 'data': None, 'msg': str(exc)}), 400
    except Exception as exc:
        return jsonify({'code': 500, 'data': None, 'msg': str(exc)}), 500
    return jsonify({'code': 200, 'data': result, 'msg': 'ok'}), 200


@app.route('/ai/pack/start', methods=['POST'])
def ai_pack_start():
    data = request.get_json(force=True) or {}
    try:
        result = ai_pack_jobs.start(data)
    except Exception as exc:
        return jsonify({'code': 500, 'data': None, 'msg': str(exc)}), 500
    return jsonify({'code': 200, 'data': result, 'msg': 'ok'}), 200


@app.route('/ai/pack/status/<int:job_id>', methods=['GET'])
def ai_pack_status(job_id):
    result = ai_pack_jobs.status(job_id)
    if not result:
        return jsonify({'code': 404, 'data': None, 'msg': 'job not found'}), 404
    return jsonify({'code': 200, 'data': result, 'msg': None}), 200


@app.route('/ai/pack/cache/clear', methods=['POST'])
def ai_pack_cache_clear():
    removed = ai_pack_jobs.clear_cache()
    return jsonify({'code': 200, 'data': {'removed': removed}, 'msg': 'ok'}), 200

@app.route('/login')
def login():
    # 1 小红书 2 视频号 3 抖音 4 快手
    type = request.args.get('type')
    # 账号名
    id = request.args.get('id')
    account_id = request.args.get('account_id', type=int)

    # id 会作为 active_queues 的键，必须限制长度，避免被恶意参数撑爆内存
    id = (id or '').strip()[:64]
    if not id:
        return jsonify({"code": 400, "msg": "id is required", "data": None}), 400
    if type not in ('1', '2', '3', '4', '5', '6'):
        return jsonify({"code": 400, "msg": "invalid platform type", "data": None}), 400

    # 每次登录使用独立会话；刷新二维码会开新会话，避免旧会话清掉新会话的验证队列。
    queue_id = (request.args.get('session') or '').strip()[:64]
    if not queue_id:
        queue_id = f"{id}-{uuid.uuid4().hex[:12]}"

    with pending_login_lock:
        login_request = pending_login_requests.pop(queue_id, None)
    if login_request and time.monotonic() - login_request["created_at"] > 120:
        login_request = None
    auth_method = (login_request or {}).get("auth_method", "qr")
    auth_phone = (login_request or {}).get("phone", "")
    auth_password = (login_request or {}).get("password", "")

    # 模拟一个用于异步通信的队列
    status_queue = Queue()
    verify_queue = Queue()
    active_queues[queue_id] = status_queue
    active_verify_queues[queue_id] = verify_queue

    # 启动异步任务线程
    thread = threading.Thread(
        target=run_async_function,
        args=(type, id, status_queue, account_id, verify_queue, auth_method, auth_phone, auth_password),
        daemon=True,
    )
    thread.start()
    response = Response(sse_stream(status_queue, queue_id), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'  # 关键：禁用 Nginx 缓冲
    response.headers['Content-Type'] = 'text/event-stream'
    # 注意：不能设置 Connection 头，WSGI/waitress 禁止逐跳头（PEP 3333），
    # 否则 /login 会直接 500，导致前端“连接服务器失败”。
    return response


@app.route('/loginPrepare', methods=['POST'])
def login_prepare():
    """暂存密码/短信登录参数，随后由 SSE 连接取走，避免密码进入 URL 和日志。"""
    data = request.get_json(silent=True) or {}
    queue_id = str(data.get('session') or '').strip()[:64]
    platform_type = str(data.get('type') or '').strip()
    account_name = str(data.get('id') or '').strip()[:64]
    auth_method = str(data.get('auth_method') or '').strip().lower()
    if not queue_id or not account_name:
        return jsonify({"code": 400, "msg": "缺少登录会话或账号名称", "data": None}), 400
    if platform_type != '3' or auth_method not in ('password', 'sms'):
        return jsonify({"code": 400, "msg": "当前平台不支持该登录方式", "data": None}), 400

    phone = re.sub(r"[\s-]", "", str(data.get('phone') or ''))[:32]
    if phone.startswith("+86"):
        phone = phone[3:]
    elif phone.startswith("86") and len(phone) == 13:
        phone = phone[2:]
    if not re.fullmatch(r"1\d{10}", phone):
        return jsonify({"code": 400, "msg": "请输入正确的 11 位手机号", "data": None}), 400

    password = str(data.get('password') or '')[:128]
    if auth_method == 'password' and not password:
        return jsonify({"code": 400, "msg": "请输入登录密码", "data": None}), 400

    now = time.monotonic()
    with pending_login_lock:
        stale = [key for key, item in pending_login_requests.items() if now - item["created_at"] > 120]
        for key in stale:
            pending_login_requests.pop(key, None)
        pending_login_requests[queue_id] = {
            "auth_method": auth_method,
            "phone": phone,
            "password": password if auth_method == 'password' else "",
            "created_at": now,
        }
    def _expire_prepared_login():
        with pending_login_lock:
            pending_login_requests.pop(queue_id, None)
    expire_timer = threading.Timer(120, _expire_prepared_login)
    expire_timer.daemon = True
    expire_timer.start()
    return jsonify({"code": 200, "msg": "登录参数已接收", "data": {"auth_method": auth_method}}), 200

@app.route('/loginVerify', methods=['POST'])
def login_verify():
    """接收抖音二次验证（手机号 + 短信验证码）并转发给正在等待的登录线程。"""
    data = request.get_json(force=True) or {}
    key = str(data.get('id') or '').strip()[:64]
    verify_queue = active_verify_queues.get(key)
    if verify_queue is None:
        return jsonify({"code": 404, "msg": "登录验证会话不存在或已结束", "data": None}), 404

    phone = re.sub(r"[\s-]", "", str(data.get('phone') or ''))[:32]
    if phone.startswith("+86"):
        phone = phone[3:]
    elif phone.startswith("86") and len(phone) == 13:
        phone = phone[2:]
    code = str(data.get('code') or '').strip()[:32]
    stage = str(data.get('stage') or '').strip().lower()
    if not phone and not code:
        return jsonify({"code": 400, "msg": "验证信息不能为空", "data": None}), 400
    if stage == 'phone' and not re.fullmatch(r"1\d{10}", phone):
        return jsonify({"code": 400, "msg": "请输入正确的 11 位手机号", "data": None}), 400
    if stage == 'code' and not (4 <= len(code) <= 8 and code.isdigit()):
        return jsonify({"code": 400, "msg": "请输入 4-8 位数字验证码", "data": None}), 400

    # 页面按钮连续触发时，前端请求可能并发到达。短时间内相同内容只入队一次，
    # 避免登录线程消费一串重复数据，也避免让用户误以为短信被重复发送。
    signature = (stage, phone, code)
    now = time.monotonic()
    with active_verify_lock:
        previous = active_verify_submissions.get(key)
        if previous and previous['signature'] == signature and now - previous['sent_at'] < 3:
            return jsonify({"code": 200, "msg": "验证信息已提交，请勿重复点击", "data": {"duplicate": True}}), 200
        active_verify_submissions[key] = {'signature': signature, 'sent_at': now}

    verify_queue.put({"phone": phone, "code": code, "stage": stage})
    return jsonify({"code": 200, "msg": "已提交验证信息", "data": {"duplicate": False}}), 200

@app.route('/postVideo', methods=['POST'])
def postVideo():
    # 获取JSON数据
    data = request.get_json()

    if not data:
        return jsonify({"code": 400, "msg": "请求数据不能为空", "data": None}), 400

    # 从JSON数据中提取fileList和accountList
    file_list = data.get('fileList', [])
    account_list = data.get('accountList', [])
    type = data.get('type')
    title = data.get('title')
    desc = data.get('desc', '') or ''  # 正文描述，与标题分开（AI 生成的长文案走这里）
    tags = data.get('tags')
    category = data.get('category')
    enableTimer = data.get('enableTimer')
    if category == 0:
        category = None
    productLink = data.get('productLink', '')
    productTitle = data.get('productTitle', '')
    thumbnail_path = data.get('thumbnail', '')
    is_draft = data.get('isDraft', False)  # 新增参数：是否保存为草稿
    content_type = str(data.get('contentType') or 'video').strip().lower()
    if content_type not in ('video', 'image-text'):
        return jsonify({"code": 400, "msg": "不支持的内容类型", "data": None}), 400
    try:
        platform_type = int(type)
    except (TypeError, ValueError):
        return jsonify({"code": 400, "msg": "不支持的平台类型", "data": None}), 400
    if content_type == 'image-text' and platform_type != 2:
        return jsonify({"code": 400, "msg": "图文发布目前仅支持视频号", "data": None}), 400

    videos_per_day = data.get('videosPerDay')
    daily_times = data.get('dailyTimes')
    start_days = data.get('startDays')

    # 参数校验
    if not file_list:
        return jsonify({"code": 400, "msg": "文件列表不能为空", "data": None}), 400
    if not account_list:
        return jsonify({"code": 400, "msg": "账号列表不能为空", "data": None}), 400
    if not type:
        return jsonify({"code": 400, "msg": "平台类型不能为空", "data": None}), 400
    if not title:
        return jsonify({"code": 400, "msg": "标题不能为空", "data": None}), 400

    # 打印获取到的数据（仅作为示例）
    logger.debug("File List: {}", file_list)
    logger.debug("Account List: {}", account_list)

    check_result = compliance.check_content(data)
    if not check_result["passed"]:
        return jsonify({
            "code": 400,
            "msg": "发布前检查未通过",
            "data": check_result
        }), 400

    # enqueue as background job (non-blocking)
    extra = {
        'category': category, 'enableTimer': enableTimer, 'videosPerDay': videos_per_day,
        'dailyTimes': daily_times, 'startDays': start_days, 'isDraft': is_draft,
        'thumbnail': thumbnail_path, 'productLink': productLink, 'productTitle': productTitle,
        'desc': desc, 'contentType': content_type,
    }
    job_id = enqueue_job(platform_type, title, tags, file_list, account_list, extra,
                         review_required=bool(data.get('reviewRequired')))
    return jsonify({
        "code": 200,
        "msg": "发布任务已加入队列",
        "data": {"job_id": job_id}
    }), 200

@app.route('/updateUserinfo', methods=['POST'])
def updateUserinfo():
    # 获取JSON数据
    data = request.get_json()

    # 从JSON数据中提取 type 和 userName
    user_id = data.get('id')
    type = data.get('type')
    userName = data.get('userName')
    try:
        # 获取数据库连接
        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()

            # 更新数据库记录
            cursor.execute('''
                           UPDATE user_info
                           SET type     = ?,
                               userName = ?
                           WHERE id = ?;
                           ''', (type, userName, user_id))
            conn.commit()

        return jsonify({
            "code": 200,
            "msg": "account update successfully",
            "data": None
        }), 200

    except Exception:
        return jsonify({
            "code": 500,
            "msg": str("update failed!"),
            "data": None
        }), 500

@app.route('/postVideoBatch', methods=['POST'])
def postVideoBatch():
    from myUtils.platform_registry import dispatch_publish
    data_list = request.get_json()

    if not isinstance(data_list, list):
        return jsonify({"code": 400, "msg": "Expected a JSON array", "data": None}), 400
    for data in data_list:
        # 从JSON数据中提取fileList和accountList
        file_list = data.get('fileList', [])
        account_list = data.get('accountList', [])
        type = data.get('type')
        title = data.get('title')
        desc = data.get('desc', '') or ''
        tags = data.get('tags')
        category = data.get('category')
        enableTimer = data.get('enableTimer')
        if category == 0:
            category = None
        productLink = data.get('productLink', '')
        productTitle = data.get('productTitle', '')
        is_draft = data.get('isDraft', False)

        videos_per_day = data.get('videosPerDay')
        daily_times = data.get('dailyTimes')
        start_days = data.get('startDays')
        # 打印获取到的数据（仅作为示例）
        logger.debug("File List: {}", file_list)
        logger.debug("Account List: {}", account_list)
        # 统一走平台注册表分发，避免各平台参数错位（历史上漏传 publish_strategy
        # 导致定时发布被静默降级成立即发布）。
        dispatch_publish(
            type,
            title=title, files=file_list, tags=tags, account_list=account_list,
            category=category, enableTimer=enableTimer,
            videosPerDay=videos_per_day, dailyTimes=daily_times, startDays=start_days,
            desc=desc, isDraft=is_draft,
            productLink=productLink, productTitle=productTitle,
        )
    # 返回响应给客户端
    return jsonify(
        {
            "code": 200,
            "msg": None,
            "data": None
        }), 200

# Cookie文件上传API
@app.route('/uploadCookie', methods=['POST'])
def upload_cookie():
    try:
        if 'file' not in request.files:
            return jsonify({
                "code": 400,
                "msg": "没有找到Cookie文件",
                "data": None
            }), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({
                "code": 400,
                "msg": "Cookie文件名不能为空",
                "data": None
            }), 400

        if not file.filename.endswith('.json'):
            return jsonify({
                "code": 400,
                "msg": "Cookie文件必须是JSON格式",
                "data": None
            }), 400

        # 获取账号信息
        account_id = request.form.get('id')
        platform = request.form.get('platform')

        if not account_id or not platform:
            return jsonify({
                "code": 400,
                "msg": "缺少账号ID或平台信息",
                "data": None
            }), 400

        # 从数据库获取账号的文件路径
        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT filePath FROM user_info WHERE id = ?', (account_id,))
            result = cursor.fetchone()

        if not result:
            return jsonify({
                "code": 500,
                "msg": "账号不存在",
                "data": None
            }), 404

        # 保存上传的Cookie文件到对应路径
        cookie_file_path = Path(BASE_DIR / "cookiesFile" / result['filePath'])
        cookie_file_path.parent.mkdir(parents=True, exist_ok=True)

        file.save(str(cookie_file_path))

        # 更新数据库中的账号信息（可选，比如更新更新时间）
        # 这里可以根据需要添加额外的处理逻辑

        return jsonify({
            "code": 200,
            "msg": "Cookie文件上传成功",
            "data": None
        }), 200

    except Exception as e:
        logger.exception("上传 Cookie 文件时出错: {}", e)
        return jsonify({
            "code": 500,
            "msg": f"上传Cookie文件失败: {str(e)}",
            "data": None
        }), 500


# Cookie文件下载API
@app.route('/downloadCookie', methods=['GET'])
def download_cookie():
    try:
        file_path = request.args.get('filePath')
        if not file_path:
            return jsonify({
                "code": 500,
                "msg": "缺少文件路径参数",
                "data": None
            }), 400

        # 验证文件路径的安全性，防止路径遍历攻击
        cookie_file_path = Path(BASE_DIR / "cookiesFile" / file_path).resolve()
        base_path = Path(BASE_DIR / "cookiesFile").resolve()

        if not cookie_file_path.is_relative_to(base_path):
            return jsonify({
                "code": 500,
                "msg": "非法文件路径",
                "data": None
            }), 400

        if not cookie_file_path.exists():
            return jsonify({
                "code": 500,
                "msg": "Cookie文件不存在",
                "data": None
            }), 404

        # 返回文件
        return send_from_directory(
            directory=str(cookie_file_path.parent),
            path=cookie_file_path.name,
            as_attachment=True
        )

    except Exception as e:
        logger.exception("下载 Cookie 文件时出错: {}", e)
        return jsonify({
            "code": 500,
            "msg": f"下载Cookie文件失败: {str(e)}",
            "data": None
        }), 500


# 包装函数：在线程中运行异步函数
def run_async_function(type, id, status_queue, account_id=None, verify_queue=None,
                       auth_method='qr', auth_phone='', auth_password=''):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        match type:
            case '1':
                coroutine = xiaohongshu_cookie_gen(id, status_queue, account_id=account_id)
            case '2':
                coroutine = get_tencent_cookie(id, status_queue, account_id=account_id)
            case '3':
                coroutine = douyin_cookie_gen(
                    id,
                    status_queue,
                    account_id=account_id,
                    verify_queue=verify_queue,
                    auth_method=auth_method,
                    phone=auth_phone,
                    password=auth_password,
                )
            case '4':
                coroutine = get_ks_cookie(id, status_queue, account_id=account_id)
            case '5':
                coroutine = weibo_cookie_gen_sse(id, status_queue)
            case '6':
                coroutine = baijiahao_cookie_gen_sse(id, status_queue)
            case _:
                raise ValueError(f"unsupported login platform type: {type}")
        loop.run_until_complete(coroutine)
    except Exception:
        # 工作线程异常也必须给 SSE 一个终态，否则前端会一直显示“请求中”。
        logger.exception("登录工作线程异常: type={} id={}", type, id)
        status_queue.put("500")
    finally:
        asyncio.set_event_loop(None)
        loop.close()

# SSE 流生成器函数
def sse_stream(status_queue, queue_id=None, max_wait_seconds=360):
    """把扫码登录的状态消息以 SSE 形式推给前端。

    - 收到终态（200/500）后主动结束流，避免客户端与线程一直挂着。
    - 用 Queue.get(timeout=...) 阻塞等待，取代 empty()+sleep 的轮询，
      既消除竞态，也避免空转占 CPU。
    - 无论正常结束还是客户端断开，都在 finally 里清理 active_queues，
      否则每扫码一次就会泄漏一个 Queue 和一个工作线程。
    """
    deadline = time.monotonic() + max_wait_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield "data: 500\n\n"
                break
            try:
                msg = status_queue.get(timeout=min(15, remaining))
            except Empty:
                # 心跳注释帧，避免 Nginx/浏览器因空闲断开长连接
                yield ": keep-alive\n\n"
                continue
            yield f"data: {msg}\n\n"
            if msg in ("200", "500"):
                break
    except GeneratorExit:
        # 客户端主动断开
        raise
    finally:
        if queue_id is not None:
            active_queues.pop(queue_id, None)
            active_verify_queues.pop(queue_id, None)
            with active_verify_lock:
                active_verify_submissions.pop(queue_id, None)
            active_verify_queues.pop(queue_id, None)
            active_verify_submissions.pop(queue_id, None)
            logger.debug("清理队列: {}", queue_id)

import publish_jobs

start_patrol()
stats_reflow.start_stats_loop(24)
douyin_dm_sync.start_background()
xiaohongshu_dm_sync.start_background()
channels_dm_sync.start_background()


def _dm_storage_maintenance():
    """启动即 checkpoint + 当天首次备份，保证重启不会读到"只存在于 WAL"的聊天记录。"""
    try:
        result = dm_ops.maintain_storage()
        health = dm_ops.storage_health()
        logger.info(
            "私信库落盘检查：checkpoint={} backup={} messages={} latest={}",
            result.get("checkpoint", {}).get("ok"),
            result.get("backup") or "-",
            health.get("counts", {}).get("messages"),
            health.get("latest_message_at") or "-",
        )
    except Exception as exc:
        logger.warning("私信库落盘检查失败：{}", exc)


def _dm_storage_loop(interval_seconds: int = 600):
    """定期做 PASSIVE checkpoint，把 WAL 内容持续合并回主库文件。"""
    while True:
        time.sleep(max(60, int(interval_seconds)))
        try:
            dm_ops.checkpoint_storage("PASSIVE")
        except Exception:
            continue


def _dm_storage_shutdown():
    try:
        dm_ops.maintain_storage()
    except Exception:
        pass


_dm_storage_maintenance()
threading.Thread(
    target=_dm_storage_loop, name="dm-storage-checkpoint", daemon=True,
).start()
atexit.register(_dm_storage_shutdown)
for _signal in ("SIGINT", "SIGTERM", "SIGBREAK"):
    handler = getattr(signal, _signal, None)
    if handler is None:
        continue
    try:
        signal.signal(handler, lambda *_args: (_dm_storage_shutdown(), sys.exit(0)))
    except (ValueError, OSError):
        continue

if __name__ == '__main__':
    if hold_system_awake():
        logger.info("Windows 防休眠锁已启用：后台运行期间系统不会自动休眠")
    elif os.name == "nt":
        logger.warning("Windows 防休眠锁启用失败；系统仍可能自动休眠")
    if not runtime_config.is_loopback_host(runtime_config.HOST):
        logger.warning(
            "后端正在监听 {}，远程访问已开启且要求 X-YunCe-Token。",
            runtime_config.HOST,
        )
    # 用 waitress 承载生产流量：Flask 自带的开发服务器没有超时/线程治理，
    # 大文件上传和 SSE 长连接下容易出现连接堆积。
    # 仍然保持单进程模型——内存队列、发布 worker、巡检线程、SSE 都在进程内。
    try:
        from waitress import serve
    except ImportError:
        logger.warning("未安装 waitress，回退到 Flask 开发服务器（仅建议本地使用）")
        app.run(host=runtime_config.HOST, port=runtime_config.PORT, threaded=True)
    else:
        serve(
            app,
            host=runtime_config.HOST,
            port=runtime_config.PORT,
            threads=16,
            # SSE 长连接需要较长的空闲超时
            channel_timeout=1800,
            # 单个请求允许的上传体积与后端 MAX_CONTENT_LENGTH 对齐
            max_request_body_size=runtime_config.MAX_CONTENT_LENGTH,
            ident="yunce",
        )
