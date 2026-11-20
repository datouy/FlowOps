#!/usr/bin/env python
"""YunCe MCP server over newline-delimited JSON-RPC stdio."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = os.environ.get("YUNCE_BASE_URL", "http://127.0.0.1:5409").rstrip("/")
TOKEN = os.environ.get("YUNCE_ACCESS_TOKEN", "")
SERVER_NAME = "yunce"
SERVER_VERSION = "2.0.0"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = {PROTOCOL_VERSION, "2025-03-26", "2024-11-05"}


def _req(
    method: str,
    path: str,
    payload: dict | None = None,
    query: dict | None = None,
) -> Any:
    """Call the REST contract and return its ``data`` envelope."""
    query_text = urllib.parse.urlencode({
        key: value for key, value in (query or {}).items()
        if value not in (None, "")
    })
    url = BASE + path + (("?" + query_text) if query_text else "")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if TOKEN:
        headers["X-YunCe-Token"] = TOKEN
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8", "ignore") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        try:
            detail = json.loads(raw).get("msg") or raw
        except (ValueError, AttributeError):
            detail = raw
        raise RuntimeError(str(detail or f"HTTP {exc.code}")[:500]) from exc
    if body.get("code") not in (None, 200, 202):
        raise RuntimeError(str(body.get("msg") or "YunCe request failed"))
    return body.get("data", body)


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS = [
    _tool(
        "post.create",
        "Create a post draft. One post can carry a main body and platform variants.",
        {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "reviewRequired": {"type": "boolean", "default": True},
            "variants": {"type": "object", "additionalProperties": {"type": "object"}},
        },
        ["title"],
    ),
    _tool(
        "post.update",
        "Update an existing post draft without changing its identity.",
        {
            "id": {"type": "integer", "minimum": 1},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "reviewRequired": {"type": "boolean"},
            "variants": {"type": "object", "additionalProperties": {"type": "object"}},
        },
        ["id"],
    ),
    _tool(
        "post.get",
        "Read one post with its platform variants, publish jobs, and current status.",
        {"id": {"type": "integer", "minimum": 1}},
        ["id"],
    ),
    _tool(
        "post.submit",
        "Submit a saved post to selected accounts. Review is enabled unless explicitly disabled.",
        {
            "id": {"type": "integer", "minimum": 1},
            "accounts": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}},
            "variants": {"type": "object", "additionalProperties": {"type": "object"}},
            "reviewRequired": {"type": "boolean", "default": True},
            "scheduledAt": {"type": "string"},
        },
        ["id", "accounts"],
    ),
    _tool(
        "inbox.list",
        "List conversations from the inbox, with optional pending or high-intent filters.",
        {
            "status": {"type": "string", "enum": ["pending", "high"]},
            "platform": {"type": "string"},
            "q": {"type": "string"},
            "page": {"type": "integer", "minimum": 1},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    ),
    _tool(
        "inbox.reply",
        "Send a text reply to one inbox conversation.",
        {
            "id": {"type": "integer", "minimum": 1},
            "text": {"type": "string", "maxLength": 2000},
        },
        ["id", "text"],
    ),
    _tool("insights.summary", "Read the operations cockpit and lead summary.", {}),
    _tool("system.health", "Read system readiness, storage, workers, and data flow health.", {}),
]


def _variant_payload(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _post_create(arguments: dict) -> Any:
    return _req("POST", "/api/v2/posts", {
        "title": str(arguments.get("title") or ""),
        "body": str(arguments.get("body") or ""),
        "files": arguments.get("files") if isinstance(arguments.get("files"), list) else [],
        "reviewRequired": bool(arguments.get("reviewRequired", True)),
        "variants": _variant_payload(arguments.get("variants")),
    })


def _post_update(arguments: dict) -> Any:
    post_id = int(arguments.get("id") or 0)
    if post_id < 1:
        raise ValueError("id must be a positive integer")
    payload = {key: arguments[key] for key in ("title", "body", "files", "reviewRequired", "variants") if key in arguments}
    return _req("PATCH", f"/api/v2/posts/{post_id}", payload)


def _post_get(arguments: dict) -> Any:
    post_id = int(arguments.get("id") or 0)
    if post_id < 1:
        raise ValueError("id must be a positive integer")
    return _req("GET", f"/api/v2/posts/{post_id}")


def _post_submit(arguments: dict) -> Any:
    post_id = int(arguments.get("id") or 0)
    accounts = arguments.get("accounts")
    if post_id < 1 or not isinstance(accounts, dict):
        raise ValueError("id and accounts are required")
    return _req("POST", f"/api/v2/posts/{post_id}/submit", {
        "accounts": accounts,
        "variants": _variant_payload(arguments.get("variants")),
        "reviewRequired": bool(arguments.get("reviewRequired", True)),
        "scheduledAt": str(arguments.get("scheduledAt") or ""),
    })


def _inbox_list(arguments: dict) -> Any:
    return _req("GET", "/dm/conversations", query={
        key: arguments.get(key) for key in ("status", "platform", "q", "page", "page_size")
    })


def _inbox_reply(arguments: dict) -> Any:
    conversation_id = int(arguments.get("id") or 0)
    text = str(arguments.get("text") or "").strip()
    if conversation_id < 1 or not text:
        raise ValueError("id and text are required")
    return _req("POST", f"/dm/conversations/{conversation_id}/reply", {"text": text})


def _insights_summary(_: dict) -> Any:
    return {
        "cockpit": _req("GET", "/api/v2/dashboard/cockpit"),
        "leads": _req("GET", "/api/v2/roi/summary"),
    }


TOOL_HANDLERS = {
    "post.create": _post_create,
    "post.update": _post_update,
    "post.get": _post_get,
    "post.submit": _post_submit,
    "inbox.list": _inbox_list,
    "inbox.reply": _inbox_reply,
    "insights.summary": _insights_summary,
    "system.health": lambda _: _req("GET", "/api/v2/system/health"),
    # Compatibility aliases for the original line-oriented prototype.
    "contents.create": lambda p: _req("POST", "/open/v1/contents", {
        "title": p.get("title", "MCP内容"), "body": p.get("body", ""), "files": p.get("files", []),
    }),
    "jobs.create": lambda p: _req("POST", "/open/v1/jobs", {
        "type": int(p.get("type", 3)), "title": p.get("title", "MCP任务"),
        "accounts": p.get("accounts", []), "files": p.get("files", []),
        "extra": p.get("extra", {}), "reviewRequired": p.get("reviewRequired", True),
    }),
    "jobs.get": lambda p: _req("GET", f"/open/v1/jobs/{p.get('id')}"),
    "cockpit.get": lambda _: _req("GET", "/api/v2/dashboard/cockpit"),
    "roi.export": lambda p: {"url": BASE + f"/api/v2/roi/export?format={p.get('format', 'csv')}"},
    "selectors.refresh": lambda _: _req("POST", "/api/v2/selectors/refresh", {}),
}


def _tool_result(data: Any, is_error: bool = False) -> dict:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"data": data},
        "isError": bool(is_error),
    }


def _error(message_id: Any, code: int, message: str, data: Any = None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def handle_message(message: dict) -> dict | None:
    """Handle one decoded JSON-RPC message. Notifications return ``None``."""
    message_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if not method:
        return None if "id" not in message else _error(message_id, -32600, "Invalid Request")

    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        requested = str(params.get("protocolVersion") or "")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0", "id": message_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "Use post.* for content, inbox.* for conversations, insights.summary for operations, and system.health for readiness.",
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOL_DEFINITIONS}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _error(message_id, -32602, f"Unknown tool: {name}")
        try:
            return {"jsonrpc": "2.0", "id": message_id, "result": _tool_result(handler(arguments))}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": message_id, "result": _tool_result(str(exc)[:500], True)}
    if method in TOOL_HANDLERS:
        # Legacy clients sent the tool name as the JSON-RPC method.
        try:
            return {"jsonrpc": "2.0", "id": message_id, "result": TOOL_HANDLERS[method](params)}
        except Exception as exc:
            return _error(message_id, -32000, str(exc)[:500])
    if "id" not in message:
        return None
    return _error(message_id, -32601, f"Method not found: {method}")


def main() -> int:
    # MCP transports JSON as UTF-8 regardless of the Windows console code page.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                response = _error(None, -32600, "Invalid Request")
            else:
                response = handle_message(message)
        except json.JSONDecodeError:
            response = _error(None, -32700, "Parse error")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
