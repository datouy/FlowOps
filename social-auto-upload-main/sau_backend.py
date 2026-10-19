import asyncio
import os
os.environ.setdefault("FLOWOPS_BACKEND", "1")
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from queue import Queue
from flask_cors import CORS
from myUtils.auth import check_cookie
from flask import Flask, request, jsonify, Response, render_template, send_from_directory
from werkzeug.utils import secure_filename
from publish_jobs import enqueue_job, start_patrol, run_patrol_once, latest_account_status
import verify_codes
from travel_ops import bp as travel_ops_bp
from conf import BASE_DIR
from myUtils.login import (get_tencent_cookie, douyin_cookie_gen, get_ks_cookie,
                           xiaohongshu_cookie_gen, weibo_cookie_gen_sse, baijiahao_cookie_gen_sse)
from myUtils.postVideo import (post_video_tencent, post_video_DouYin, post_video_ks,
                               post_video_xhs, post_video_weibo, post_video_baijiahao)


active_queues = {}
app = Flask(__name__)
app.register_blueprint(travel_ops_bp)
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
import compliance
compliance.register(app)
import media_ops
media_ops.register(app)
import schedule_suggest
schedule_suggest.register(app)
import material_ops
material_ops.register(app)

#允许所有来源跨域访问
CORS(app)

# 限制上传文件大小为160MB
app.config['MAX_CONTENT_LENGTH'] = 160 * 1024 * 1024

# 获取当前目录（假设 index.html 和 assets 在这里）
current_dir = os.path.dirname(os.path.abspath(__file__))

# 处理所有静态资源请求（未来打包用）
@app.route('/assets/<filename>')
def custom_static(filename):
    return send_from_directory(os.path.join(current_dir, 'assets'), filename)

# 处理 favicon.ico 静态资源（未来打包用）
@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(current_dir, 'assets'), 'vite.svg')

@app.route('/vite.svg')
def vite_svg():
    return send_from_directory(os.path.join(current_dir, 'assets'), 'vite.svg')

# （未来打包用）
@app.route('/')
def index():  # put application's code here
    return send_from_directory(current_dir, 'index.html')

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
        print(f"UUID v1: {uuid_v1}")
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
        print(f"UUID v1: {uuid_v1}")

        # 构造文件名和路径
        final_filename = f"{uuid_v1}_{filename}"
        filepath = Path(BASE_DIR / "videoFile" / f"{uuid_v1}_{filename}")

        # 保存文件
        file.save(filepath)
        file_md5 = material_ops.file_md5(filepath)
        duplicate = material_ops.duplicate_count(file_md5) > 0

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                                INSERT INTO file_records (filename, filesize, file_path, md5, tags)
            VALUES (?, ?, ?, ?, ?)
                                ''', (filename, round(float(os.path.getsize(filepath)) / (1024 * 1024),2), final_filename, file_md5, ''))
            conn.commit()
            print("✅ 上传文件已记录")

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
        print(f"Upload failed: {e}")
        return jsonify({
            "code": 500,
            "msg": f"upload failed: {e}",
            "data": None
        }), 500

@app.route('/getFiles', methods=['GET'])
def get_all_files():
    try:
        # 使用 with 自动管理数据库连接
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row  # 允许通过列名访问结果
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
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str("get file failed!"),
            "data": None
        }), 500


@app.route("/getAccounts", methods=['GET'])
def getAccounts():
    """快速获取所有账号信息，不进行cookie验证"""
    try:
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
            SELECT * FROM user_info''')
            rows = cursor.fetchall()
            rows_list = [list(row) for row in rows]

            print("\n📋 当前数据表内容（快速获取）：")
            for row in rows:
                print(row)

            return jsonify(
                {
                    "code": 200,
                    "msg": None,
                    "data": rows_list
                }), 200
    except Exception as e:
        print(f"获取账号列表时出错: {str(e)}")
        return jsonify({
            "code": 500,
            "msg": f"获取账号列表失败: {str(e)}",
            "data": None
        }), 500


@app.route("/getValidAccounts",methods=['GET'])
async def getValidAccounts():
    with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
        cursor = conn.cursor()
        cursor.execute('''
        SELECT * FROM user_info''')
        rows = cursor.fetchall()
        rows_list = [list(row) for row in rows]
        print("\n📋 当前数据表内容：")
        for row in rows:
            print(row)
        for row in rows_list:
            flag = await check_cookie(row[1],row[2])
            if not flag:
                row[4] = 0
                cursor.execute('''
                UPDATE user_info 
                SET status = ? 
                WHERE id = ?
                ''', (0,row[0]))
                conn.commit()
                print("✅ 用户状态已更新")
        for row in rows:
            print(row)
        return jsonify(
                        {
                            "code": 200,
                            "msg": None,
                            "data": rows_list
                        }),200

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
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
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
                    print(f"✅ 实际文件已删除: {file_path}")
                except Exception as e:
                    print(f"⚠️ 删除实际文件失败: {e}")
                    # 即使删除文件失败，也要继续删除数据库记录，避免数据不一致
            else:
                print(f"⚠️ 实际文件不存在: {file_path}")

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

    except Exception as e:
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
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 查询要删除的记录
            cursor.execute("SELECT * FROM user_info WHERE id = ?", (account_id,))
            record = cursor.fetchone()

            if not record:
                return jsonify({
                    "code": 404,
                    "msg": "account not found",
                    "data": None
                }), 404

            record = dict(record)

            # 删除关联的cookie文件
            if record.get('filePath'):
                cookie_file_path = Path(BASE_DIR / "cookiesFile" / record['filePath'])
                if cookie_file_path.exists():
                    try:
                        cookie_file_path.unlink()
                        print(f"✅ Cookie文件已删除: {cookie_file_path}")
                    except Exception as e:
                        print(f"⚠️ 删除Cookie文件失败: {e}")

            # 删除数据库记录
            cursor.execute("DELETE FROM user_info WHERE id = ?", (account_id,))
            conn.commit()

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
            conn = sqlite3.connect(Path(BASE_DIR / "db" / "database.db"))
            conn.row_factory = sqlite3.Row
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
    return jsonify({'code': 200, 'data': rows, 'msg': None}), 200

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
            import traceback; traceback.print_exc()
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

    # 模拟一个用于异步通信的队列
    status_queue = Queue()
    active_queues[id] = status_queue

    def on_close():
        print(f"清理队列: {id}")
        del active_queues[id]
    # 启动异步任务线程
    thread = threading.Thread(target=run_async_function, args=(type,id,status_queue), daemon=True)
    thread.start()
    response = Response(sse_stream(status_queue,), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'  # 关键：禁用 Nginx 缓冲
    response.headers['Content-Type'] = 'text/event-stream'
    response.headers['Connection'] = 'keep-alive'
    return response

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
    tags = data.get('tags')
    category = data.get('category')
    enableTimer = data.get('enableTimer')
    if category == 0:
        category = None
    productLink = data.get('productLink', '')
    productTitle = data.get('productTitle', '')
    thumbnail_path = data.get('thumbnail', '')
    is_draft = data.get('isDraft', False)  # 新增参数：是否保存为草稿

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
    print("File List:", file_list)
    print("Account List:", account_list)

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
        'thumbnail': thumbnail_path, 'productLink': productLink, 'productTitle': productTitle
    }
    job_id = enqueue_job(type, title, tags, file_list, account_list, extra,
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
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
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

    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str("update failed!"),
            "data": None
        }), 500

@app.route('/postVideoBatch', methods=['POST'])
def postVideoBatch():
    data_list = request.get_json()

    if not isinstance(data_list, list):
        return jsonify({"code": 400, "msg": "Expected a JSON array", "data": None}), 400
    for data in data_list:
        # 从JSON数据中提取fileList和accountList
        file_list = data.get('fileList', [])
        account_list = data.get('accountList', [])
        type = data.get('type')
        title = data.get('title')
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
        print("File List:", file_list)
        print("Account List:", account_list)
        match type:
            case 1:
                post_video_xhs(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                               start_days)
            case 2:
                post_video_tencent(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                                   start_days, is_draft)
            case 3:
                post_video_DouYin(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days, productLink, productTitle)
            case 4:
                post_video_ks(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days)
            case 5:
                post_video_weibo(title, file_list, tags, account_list)
            case 6:
                post_video_baijiahao(title, file_list, tags, account_list)
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
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
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
        print(f"上传Cookie文件时出错: {str(e)}")
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
        print(f"下载Cookie文件时出错: {str(e)}")
        return jsonify({
            "code": 500,
            "msg": f"下载Cookie文件失败: {str(e)}",
            "data": None
        }), 500


# 包装函数：在线程中运行异步函数
def run_async_function(type,id,status_queue):
    match type:
        case '1':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(xiaohongshu_cookie_gen(id, status_queue))
            loop.close()
        case '2':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(get_tencent_cookie(id,status_queue))
            loop.close()
        case '3':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(douyin_cookie_gen(id,status_queue))
            loop.close()
        case '4':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(get_ks_cookie(id,status_queue))
            loop.close()
        case '5':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(weibo_cookie_gen_sse(id, status_queue))
            loop.close()
        case '6':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(baijiahao_cookie_gen_sse(id, status_queue))
            loop.close()

# SSE 流生成器函数
def sse_stream(status_queue):
    while True:
        if not status_queue.empty():
            msg = status_queue.get()
            yield f"data: {msg}\n\n"
        else:
            # 避免 CPU 占满
            time.sleep(0.1)

import publish_jobs

start_patrol()
stats_reflow.start_stats_loop(24)

if __name__ == '__main__':
    app.run(host='0.0.0.0' ,port=5409)
