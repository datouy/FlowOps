from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_content  # noqa: E402
import ai_usage  # noqa: E402


def test_ai_requires_explicit_https_endpoint(monkeypatch):
    # Importing the backend loads the deployment .env into os.environ. This
    # case verifies the default fail-closed rule, so opt-out must be explicit.
    monkeypatch.delenv("FLOWOPS_AI_ALLOW_INSECURE", raising=False)
    monkeypatch.setattr(ai_content, "_load_env", lambda: {
        "WB_API_KEY": "key",
        "WB_BASE_URL": "http://public.example/v1",
        "WB_MODEL": "model",
        "WB_REALM": "cn",
    })
    status = ai_content.config_status()
    assert status["configured"] is False
    assert "HTTPS" in status["error"]

    monkeypatch.setattr(ai_content, "_load_env", lambda: {
        "WB_API_KEY": "key",
        "WB_BASE_URL": "https://ai.example/v1",
        "WB_MODEL": "model",
        "WB_REALM": "cn",
    })
    assert ai_content.config_status()["configured"] is True


def test_ai_loopback_http_is_allowed(monkeypatch):
    monkeypatch.delenv("FLOWOPS_AI_ALLOW_INSECURE", raising=False)
    assert ai_content._validated_base_url("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080/v1"


def test_ai_usage_records_tokens_and_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_usage, "DB", tmp_path / "ai_usage.db")
    monkeypatch.setenv("WB_INPUT_COST_PER_1M", "2")
    monkeypatch.setenv("WB_OUTPUT_COST_PER_1M", "4")
    ai_usage.init_db()
    ai_usage.record_call(
        scene="content",
        caller="local",
        model="demo",
        duration_ms=12,
        usage={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
    )
    summary = ai_usage.summary(30)
    assert summary["totals"]["total_tokens"] == 1500
    assert summary["totals"]["cost"] == pytest.approx(0.004)
    assert summary["byScene"][0]["scene"] == "content"


def test_ai_daily_quota_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_usage, "DB", tmp_path / "ai_usage.db")
    monkeypatch.setenv("FLOWOPS_AI_DAILY_LIMIT", "1")
    ai_usage.init_db()
    assert ai_usage.check_quota()[0] is True
    ai_usage.record_call(scene="content", caller="local", model="demo", duration_ms=1)
    ok, reason = ai_usage.check_quota()
    assert ok is False
    assert "上限 1" in reason
