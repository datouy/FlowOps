from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_mcp_initialize_and_tool_listing():
    import mcp_server

    initialized = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26"},
    })
    assert initialized["result"]["protocolVersion"] == "2025-03-26"
    assert initialized["result"]["capabilities"]["tools"]["listChanged"] is False
    assert mcp_server.handle_message({
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }) is None

    listed = mcp_server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {item["name"] for item in listed["result"]["tools"]}
    assert {"post.create", "post.update", "post.get", "post.submit"} <= names
    assert {"inbox.list", "inbox.reply", "insights.summary", "system.health"} <= names
    assert all(item["inputSchema"]["type"] == "object" for item in listed["result"]["tools"])


def test_mcp_tool_call_and_legacy_method(monkeypatch):
    import mcp_server

    calls = []

    def fake_req(method, path, payload=None, query=None):
        calls.append((method, path, payload, query))
        return {"id": 7}

    monkeypatch.setattr(mcp_server, "_req", fake_req)
    response = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {
            "name": "post.create",
            "arguments": {"title": "MCP post", "body": "body", "files": ["a.jpg"]},
        },
    })
    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["data"] == {"id": 7}
    assert calls[0][0:2] == ("POST", "/api/v2/posts")
    assert calls[0][2]["reviewRequired"] is True

    legacy = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 4, "method": "contents.create",
        "params": {"title": "legacy", "body": "body"},
    })
    assert legacy["result"] == {"id": 7}
    assert calls[1][0:2] == ("POST", "/open/v1/contents")
