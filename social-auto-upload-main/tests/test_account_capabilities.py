from __future__ import annotations

from account_capabilities import build_account_capabilities


def _account(platform_type=2, *, status=1, cookie=True, identity=True):
    return {
        "status": status,
        "failReason": "" if status == 1 else "登录态失效",
        "platform": {"type": platform_type},
        "cookie": {"exists": cookie},
        "identity": {
            "bound": identity,
            "masked": "v2_060…86" if identity else "",
        },
    }


def test_creator_login_does_not_make_main_site_dm_healthy():
    capabilities = build_account_capabilities(
        _account(),
        {
            "errorKind": "account_expired",
            "lastError": "主站私信登录已失效",
            "lastSuccessAt": "",
        },
    )

    assert capabilities["login"]["ok"] is True
    assert capabilities["publish"]["ok"] is True
    assert capabilities["dm"]["ok"] is False
    assert capabilities["dm"]["state"] == "blocked"
    assert "主站" in capabilities["dm"]["reason"]


def test_expired_login_blocks_account_actions_but_keeps_identity_separate():
    capabilities = build_account_capabilities(_account(status=0, cookie=False, identity=True))

    assert capabilities["login"]["state"] == "blocked"
    assert capabilities["publish"]["state"] == "blocked"
    assert capabilities["verification"]["state"] == "blocked"
    assert capabilities["comments"]["state"] == "blocked"
    assert capabilities["identity"]["state"] == "ready"
    assert capabilities["dm"]["state"] == "unknown"


def test_matrix_marks_unsupported_platform_features_explicitly():
    capabilities = build_account_capabilities(_account(5, identity=False))

    assert capabilities["dm"]["state"] == "unsupported"
    assert capabilities["comments"]["state"] == "unsupported"
    assert capabilities["verification"]["state"] == "unsupported"
    assert set(capabilities) == {
        "login", "publish", "verification", "comments", "dm", "identity"
    }
