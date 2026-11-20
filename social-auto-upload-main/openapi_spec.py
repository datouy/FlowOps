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
            {"name": "Content"}, {"name": "Inbox"}, {"name": "Insights"}, {"name": "System"},
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
