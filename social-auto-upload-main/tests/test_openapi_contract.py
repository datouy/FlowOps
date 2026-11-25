from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_openapi_document_exposes_unified_post_contract():
    import openapi_spec

    app = Flask(__name__)
    openapi_spec.register(app)
    response = app.test_client().get("/openapi.json")
    assert response.status_code == 200
    spec = response.get_json()
    assert spec["openapi"] == "3.1.0"
    assert {
        "/api/v2/posts", "/api/v2/posts/{id}", "/api/v2/posts/{id}/submit",
        "/dm/conversations", "/dm/conversations/{id}/reply",
        "/api/v2/auth/login", "/api/v2/auth/logout", "/api/v2/users",
        "/api/v2/auth/mfa/verify", "/api/v2/auth/mfa/setup",
        "/api/v2/users/{id}", "/api/v2/users/{id}/mfa-reset",
        "/api/v2/users/{id}/queued-jobs",
        "/api/v2/users/{id}/cancel-queued-jobs",
        "/api/v2/users/{id}/unlock-login", "/api/metrics",
    } <= set(spec["paths"])
    operation_ids = [
        operation["operationId"]
        for path in spec["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))
    assert "X-YunCe-Token" in spec["components"]["securitySchemes"]["YunCeToken"]["name"]
