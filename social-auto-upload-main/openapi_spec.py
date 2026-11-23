"""OpenAPI contract for the stable REST and agent-facing endpoints."""
from __future__ import annotations

from flask import jsonify, request


def _json_response(schema: str, description: str = "OK") -> dict:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}},
    }


def _request_body(schema: str, required: bool = True) -> dict:
    return {
        "required": required,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}},
    }


def _path_id(name: str = "id") -> dict:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "schema": {"type": "integer", "minimum": 1},
    }


def build_spec(server_url: str) -> dict:
    """Return a deterministic OpenAPI document for the public integration surface."""
    platform_variant = {
        "type": "object",
        "additionalProperties": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "scheduledAt": {"type": "string", "description": "Local date-time: YYYY-MM-DD HH:mm"},
            },
        },
    }
    schemas = {
        "ErrorEnvelope": {
            "type": "object",
            "required": ["code", "msg"],
            "properties": {
                "code": {"type": "integer"},
                "msg": {"type": "string"},
                "data": {"nullable": True},
            },
        },
        "PostInput": {
            "type": "object",
            "required": ["title"],
            "properties": {
                "title": {"type": "string", "maxLength": 120},
                "body": {"type": "string", "maxLength": 4000},
                "files": {"type": "array", "items": {"type": "string"}},
                "reviewRequired": {"type": "boolean", "default": True},
                "variants": platform_variant,
            },
        },
        "PostPatch": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 120},
                "body": {"type": "string", "maxLength": 4000},
                "files": {"type": "array", "items": {"type": "string"}},
                "reviewRequired": {"type": "boolean"},
                "variants": platform_variant,
            },
        },
        "Post": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "review_required": {"type": "boolean"},
                "status": {"enum": ["draft", "review", "scheduled", "publishing", "published", "attention"]},
                "variants": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "jobs": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "created_at": {"type": "string"},
                "updated_at": {"type": "string"},
            },
        },
        "PostResponse": {
            "type": "object",
            "properties": {
                "code": {"type": "integer", "example": 200},
                "msg": {"nullable": True},
                "data": {"$ref": "#/components/schemas/Post"},
            },
        },
        "PostListResponse": {
            "type": "object",
            "properties": {
                "code": {"type": "integer", "example": 200},
                "msg": {"nullable": True},
                "data": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": {"$ref": "#/components/schemas/Post"}},
                        "total": {"type": "integer"},
                    },
                },
            },
        },
        "PostSubmitRequest": {
            "type": "object",
            "required": ["accounts"],
            "properties": {
                "accounts": {
                    "type": "object",
                    "description": "Platform key to account cookie filenames.",
                    "additionalProperties": {"type": "array", "items": {"type": "string"}},
                },
                "variants": platform_variant,
                "reviewRequired": {"type": "boolean", "default": True},
                "scheduledAt": {"type": "string"},
            },
        },
        "JobEnvelope": {"type": "object", "additionalProperties": True},
        "HealthEnvelope": {"type": "object", "additionalProperties": True},
        "ReplyInput": {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string", "maxLength": 2000}},
        },
        "AuthLoginInput": {
            "type": "object",
            "required": ["username", "password"],
            "properties": {
                "username": {"type": "string", "minLength": 3, "maxLength": 64},
                "password": {"type": "string", "minLength": 12, "maxLength": 256},
            },
        },
        "PasswordChangeInput": {
            "type": "object",
            "required": ["currentPassword", "newPassword"],
            "properties": {
                "currentPassword": {"type": "string"},
                "newPassword": {"type": "string", "minLength": 12, "maxLength": 256},
            },
        },
        "PasswordResetInput": {
            "type": "object",
            "required": ["password"],
            "properties": {
                "password": {"type": "string", "minLength": 12, "maxLength": 256},
                "mustChangePassword": {"type": "boolean", "default": True},
            },
        },
        "MfaVerifyInput": {
            "type": "object",
            "required": ["challengeToken", "code"],
            "properties": {
                "challengeToken": {"type": "string"},
                "code": {"type": "string", "pattern": "^[0-9]{6}$"},
            },
        },
        "MfaSetupInput": {
            "type": "object",
            "required": ["currentPassword"],
            "properties": {"currentPassword": {"type": "string"}},
        },
        "MfaConfirmInput": {
            "type": "object",
            "required": ["code"],
            "properties": {"code": {"type": "string", "pattern": "^[0-9]{6}$"}},
        },
        "MfaDisableInput": {
            "type": "object",
            "required": ["currentPassword", "code"],
            "properties": {
                "currentPassword": {"type": "string"},
                "code": {"type": "string", "pattern": "^[0-9]{6}$"},
            },
        },
        "NamedUserInput": {
            "type": "object",
            "required": ["username", "password", "role", "scope"],
            "properties": {
                "username": {"type": "string", "minLength": 3, "maxLength": 64},
                "displayName": {"type": "string", "maxLength": 100},
                "password": {"type": "string", "minLength": 12, "maxLength": 256},
                "role": {"enum": ["cutter", "reviewer", "operator", "admin"]},
                "scope": {"type": "array", "items": {"type": "string"}},
                "mustChangePassword": {"type": "boolean", "default": True},
            },
        },
        "NamedUserPatch": {
            "type": "object",
            "properties": {
                "displayName": {"type": "string", "maxLength": 100},
                "role": {"enum": ["cutter", "reviewer", "operator", "admin"]},
                "scope": {"type": "array", "items": {"type": "string"}},
                "disabled": {"type": "boolean"},
            },
        },
    }
    common_errors = {
        "400": _json_response("ErrorEnvelope", "Invalid request"),
        "401": _json_response("ErrorEnvelope", "Missing or invalid access token"),
        "404": _json_response("ErrorEnvelope", "Resource not found"),
    }
    paths = {
        "/api/v2/posts": {
            "get": {
                "operationId": "listPosts",
                "summary": "List posts from the unified content library",
                "parameters": [{
                    "name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                }],
                "responses": {"200": _json_response("PostListResponse"), **common_errors},
            },
            "post": {
                "operationId": "createPost",
                "summary": "Create a post draft",
                "requestBody": _request_body("PostInput"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/posts/{id}": {
            "parameters": [_path_id()],
            "get": {
                "operationId": "getPost",
                "summary": "Read one post with platform variants and jobs",
                "responses": {"200": _json_response("PostResponse"), **common_errors},
            },
            "patch": {
                "operationId": "updatePost",
                "summary": "Update a post draft",
                "requestBody": _request_body("PostPatch"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/posts/{id}/submit": {
            "parameters": [_path_id()],
            "post": {
                "operationId": "submitPost",
                "summary": "Create one publish job per selected platform and account",
                "requestBody": _request_body("PostSubmitRequest"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/dm/conversations": {
            "get": {
                "operationId": "listConversations",
                "summary": "List inbox conversations",
                "parameters": [
                    {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["pending", "high"]}},
                    {"name": "platform", "in": "query", "schema": {"type": "string"}},
                    {"name": "q", "in": "query", "schema": {"type": "string"}},
                    {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                    {"name": "page_size", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 200}},
                ],
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/dm/conversations/{id}/reply": {
            "parameters": [_path_id()],
            "post": {
                "operationId": "replyConversation",
                "summary": "Send one reply to a conversation",
                "requestBody": _request_body("ReplyInput"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/dashboard/cockpit": {
            "get": {
                "operationId": "getOperationsSummary",
                "summary": "Read the operations cockpit",
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/system/health": {
            "get": {
                "operationId": "getSystemHealth",
                "summary": "Read database and background worker health",
                "responses": {"200": _json_response("HealthEnvelope"), **common_errors},
            },
        },
        "/api/v2/auth/config": {
            "get": {
                "operationId": "getAuthConfig",
                "summary": "Read public login capabilities and password policy",
                "security": [],
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/auth/login": {
            "post": {
                "operationId": "loginNamedUser",
                "summary": "Create a revocable named-user session",
                "security": [],
                "requestBody": _request_body("AuthLoginInput"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/auth/logout": {
            "post": {
                "operationId": "logoutNamedUser",
                "summary": "Revoke the current named-user session",
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/auth/mfa/verify": {
            "post": {
                "operationId": "verifyMfaLogin",
                "summary": "Exchange a password challenge and TOTP code for a session",
                "security": [],
                "requestBody": _request_body("MfaVerifyInput"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/auth/mfa/setup": {
            "post": {
                "operationId": "setupMfa",
                "summary": "Generate an encrypted pending TOTP secret",
                "requestBody": _request_body("MfaSetupInput"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/auth/mfa/confirm": {
            "post": {
                "operationId": "confirmMfa",
                "summary": "Confirm TOTP enrollment and revoke other sessions",
                "requestBody": _request_body("MfaConfirmInput"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/auth/mfa/disable": {
            "post": {
                "operationId": "disableMfa",
                "summary": "Disable TOTP after password and code verification",
                "requestBody": _request_body("MfaDisableInput"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/auth/password": {
            "post": {
                "operationId": "changeOwnPassword",
                "summary": "Change the current password and revoke all sessions",
                "requestBody": _request_body("PasswordChangeInput"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/auth/status": {
            "get": {
                "operationId": "getAuthStatus",
                "summary": "Read named-user and session totals",
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/users": {
            "get": {
                "operationId": "listNamedUsers",
                "summary": "List named users and account scopes",
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
            "post": {
                "operationId": "createNamedUser",
                "summary": "Create a named user with a production role",
                "requestBody": _request_body("NamedUserInput"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/users/{id}": {
            "parameters": [_path_id()],
            "patch": {
                "operationId": "updateNamedUser",
                "summary": "Change role, scope, display name or disabled state",
                "requestBody": _request_body("NamedUserPatch"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/users/{id}/password": {
            "parameters": [_path_id()],
            "post": {
                "operationId": "resetNamedUserPassword",
                "summary": "Reset a password and revoke all sessions",
                "requestBody": _request_body("PasswordResetInput"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/users/{id}/revoke-sessions": {
            "parameters": [_path_id()],
            "post": {
                "operationId": "revokeNamedUserSessions",
                "summary": "Force a named user offline",
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/users/{id}/mfa-reset": {
            "parameters": [_path_id()],
            "post": {
                "operationId": "resetNamedUserMfa",
                "summary": "Reset a user's MFA and revoke sessions",
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/v2/users/{id}/unlock-login": {
            "parameters": [_path_id()],
            "post": {
                "operationId": "unlockNamedUserLogin",
                "summary": "Clear a user's persistent password login lock",
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/api/metrics": {
            "get": {
                "operationId": "getPrometheusMetrics",
                "summary": "Read Prometheus text metrics",
                "responses": {
                    "200": {
                        "description": "Prometheus text exposition",
                        "content": {"text/plain": {"schema": {"type": "string"}}},
                    },
                    **common_errors,
                },
            },
        },
        "/api/v2/roi/summary": {
            "get": {
                "operationId": "getLeadSummary",
                "summary": "Read lead and conversion totals",
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
        "/open/v1/jobs": {
            "post": {
                "operationId": "createLegacyJob",
                "summary": "Create a publish job through the compatibility API",
                "requestBody": _request_body("JobEnvelope"),
                "responses": {"200": _json_response("JobEnvelope"), **common_errors},
            },
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "YunCe Content Workspace API",
            "version": "2.0.0",
            "description": "Stable REST contract shared by the web workspace and the MCP server.",
        },
        "servers": [{"url": str(server_url or "http://127.0.0.1:5409").rstrip("/")}],
        "security": [{"YunCeToken": []}],
        "tags": [
            {"name": "Content"}, {"name": "Inbox"}, {"name": "Insights"},
            {"name": "Auth"}, {"name": "System"},
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "YunCeToken": {"type": "apiKey", "in": "header", "name": "X-YunCe-Token"},
            },
            "schemas": schemas,
        },
    }


def register(app) -> None:
    @app.route("/openapi.json", methods=["GET"])
    def openapi_document():
        response = jsonify(build_spec(request.url_root))
        response.headers["Cache-Control"] = "no-store"
        return response, 200
