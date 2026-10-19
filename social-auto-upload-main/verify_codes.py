# -*- coding: utf-8 -*-
"""短信验证码桥接：发布线程等待验证码，网页发布界面负责输入。"""
import os
import threading
import time
import uuid

MAX_REQUEST_AGE_SECONDS = 30 * 60

_lock = threading.Lock()
_requests = {}
_events = {}
_context = threading.local()


def set_job_context(job_id=None, title="", platform="", file_name=""):
    _context.job_id = job_id
    _context.title = title or ""
    _context.platform = platform or ""
    _context.file_name = file_name or ""


def clear_job_context():
    for attr in ("job_id", "title", "platform", "file_name"):
        try:
            delattr(_context, attr)
        except AttributeError:
            pass


def is_backend_mode():
    return os.environ.get("FLOWOPS_BACKEND") == "1"


def _job_context():
    return {
        "job_id": getattr(_context, "job_id", None),
        "title": getattr(_context, "title", ""),
        "platform": getattr(_context, "platform", ""),
        "file_name": getattr(_context, "file_name", ""),
    }


def _snapshot(request):
    return {key: value for key, value in request.items() if key != "code"}


def create_request(platform="", account_file="", account_name=""):
    """Create (or reuse) a verification request for the current worker thread."""
    now = time.time()
    context = _job_context()
    account_file = str(account_file or "")
    with _lock:
        for request in _requests.values():
            if (
                request["status"] in ("pending", "verifying")
                and request.get("job_id") == context["job_id"]
                and request.get("account_file") == account_file
                and now - request["updated_at"] < MAX_REQUEST_AGE_SECONDS
            ):
                return _snapshot(request)

        request_id = uuid.uuid4().hex
        request = {
            "id": request_id,
            "job_id": context["job_id"],
            "title": context["title"],
            "file_name": context["file_name"],
            "platform": platform or context["platform"] or "抖音",
            "account_file": account_file,
            "account_name": account_name or "",
            "status": "pending",
            "message": "平台要求短信验证，请查看手机短信并输入验证码",
            "error": "",
            "resend_requested": False,
            "created_at": now,
            "updated_at": now,
        }
        _requests[request_id] = request
        _events[request_id] = threading.Event()
        return _snapshot(request)


def latest_pending():
    """Return the oldest unresolved request, if any."""
    with _lock:
        pending = [request for request in _requests.values() if request["status"] == "pending"]
        if not pending:
            return None
        pending.sort(key=lambda item: item["created_at"])
        return _snapshot(pending[0])


def submit_code(request_id, code):
    with _lock:
        request = _requests.get(request_id)
        if not request:
            return False, "验证码请求不存在或已结束"
        if request["status"] != "pending":
            return False, "该验证码请求已处理，请等待最新弹窗"
        request["code"] = code
        request["status"] = "submitted"
        request["error"] = ""
        request["updated_at"] = time.time()
        event = _events.get(request_id)
    if event:
        event.set()
    return True, ""


def poll_code(request_id):
    """Consume a submitted code. Called by the publishing worker."""
    with _lock:
        request = _requests.get(request_id)
        if not request:
            return ""
        if request["status"] != "submitted":
            return ""
        code = request.get("code") or ""
        request["code"] = ""
        request["status"] = "verifying"
        request["updated_at"] = time.time()
        return code


def report_result(request_id, success, error=""):
    with _lock:
        request = _requests.get(request_id)
        if not request:
            return
        now = time.time()
        request["updated_at"] = now
        if success:
            request["status"] = "resolved"
            request["error"] = ""
        else:
            request["status"] = "pending"
            request["error"] = error or "验证码验证失败，请重新输入"
            request["resend_requested"] = True


def request_resend(request_id):
    with _lock:
        request = _requests.get(request_id)
        if not request:
            return False, "验证码请求不存在或已结束"
        if request["status"] != "pending":
            return False, "当前无需重新发送验证码"
        request["resend_requested"] = True
        request["error"] = ""
        request["updated_at"] = time.time()
        return True, ""


def consume_resend(request_id):
    with _lock:
        request = _requests.get(request_id)
        if not request or not request.get("resend_requested"):
            return False
        request["resend_requested"] = False
        return True


def cancel_job_requests(job_id, reason=""):
    """Close any outstanding request once its publishing job stops running."""
    if job_id is None:
        return
    with _lock:
        for request in _requests.values():
            if request.get("job_id") != job_id:
                continue
            if request["status"] in ("resolved", "cancelled"):
                continue
            request["status"] = "cancelled"
            request["error"] = reason or request.get("error") or ""
            request["updated_at"] = time.time()
