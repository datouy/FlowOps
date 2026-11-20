"""Per-account capability matrix shared by the account API and UI."""
from __future__ import annotations

from typing import Any

from comment_ops import COMMENT_PAGES
from myUtils.platform_registry import PUBLISHERS, supports_verification


DM_PLATFORMS = frozenset({1, 2, 3})


def _item(ok: bool, state: str, reason: str = "") -> dict[str, Any]:
    return {"ok": bool(ok), "state": state, "reason": str(reason or "")[:300]}


def _platform_action(
    supported: bool,
    login_ready: bool,
    login_reason: str,
    *,
    unsupported_reason: str,
    blocked_reason: str,
    ready_reason: str,
) -> dict[str, Any]:
    if not supported:
        return _item(False, "unsupported", unsupported_reason)
    if not login_ready:
        return _item(False, "blocked", blocked_reason or login_reason)
    return _item(True, "ready", ready_reason)


def build_account_capabilities(
    account: dict[str, Any], dm_state: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Calculate login, publish, verification, comments, DM and identity.

    Each capability is derived from its own evidence. In particular, a healthy
    creator-login cookie does not make main-site DM healthy, and either one does
    not imply that comments or remote verification are available.
    """
    platform_type = int((account.get("platform") or {}).get("type") or 0)
    cookie_exists = bool((account.get("cookie") or {}).get("exists"))
    status = int(account.get("status") or 0)
    fail_reason = str(account.get("failReason") or "")
    login_ready = cookie_exists and status == 1
    if not cookie_exists:
        login_reason = "登录凭据文件不存在，请重新登录"
    elif status != 1:
        login_reason = fail_reason or "发布登录会话未通过健康检查"
    else:
        login_reason = "登录凭据存在且健康检查正常"

    capabilities = {
        "login": _item(
            login_ready,
            "ready" if login_ready else "blocked",
            login_reason,
        ),
        "publish": _platform_action(
            platform_type in PUBLISHERS,
            login_ready,
            login_reason,
            unsupported_reason="当前平台没有注册发布入口",
            blocked_reason=f"发布登录不可用：{login_reason}",
            ready_reason="已注册发布入口，发布登录正常",
        ),
        "verification": _platform_action(
            supports_verification(platform_type),
            login_ready,
            login_reason,
            unsupported_reason="当前平台尚未接入远端作品回查",
            blocked_reason=f"作品回查需要有效登录：{login_reason}",
            ready_reason="已接入远端作品状态回查",
        ),
        "comments": _platform_action(
            platform_type in COMMENT_PAGES,
            login_ready,
            login_reason,
            unsupported_reason="当前平台尚未接入评论管理",
            blocked_reason=f"评论能力需要有效登录：{login_reason}",
            ready_reason="已接入评论同步与评论操作",
        ),
    }

    if platform_type not in DM_PLATFORMS:
        capabilities["dm"] = _item(
            False, "unsupported", "当前平台尚未接入主站私信同步"
        )
    elif not dm_state:
        capabilities["dm"] = _item(False, "unknown", "尚未取得该账号的私信健康状态")
    else:
        error_kind = str(dm_state.get("errorKind") or "")
        error = str(dm_state.get("lastError") or "")
        last_success = str(dm_state.get("lastSuccessAt") or "")
        if error_kind == "account_expired":
            capabilities["dm"] = _item(
                False, "blocked", error or "主站私信登录已失效，请重新登录"
            )
        elif error_kind:
            capabilities["dm"] = _item(
                False, "blocked", error or "私信同步异常"
            )
        elif last_success:
            capabilities["dm"] = _item(
                True, "ready", f"最近成功同步：{last_success}"
            )
        else:
            capabilities["dm"] = _item(
                False, "unknown", "主站会话存在，但还没有成功同步记录"
            )

    identity = account.get("identity") or {}
    identity_bound = bool(identity.get("bound"))
    capabilities["identity"] = _item(
        identity_bound,
        "ready" if identity_bound else "blocked",
        (
            f"已绑定平台身份：{identity.get('masked') or ''}"
            if identity_bound
            else "尚未绑定平台侧稳定身份，请重新登录或回填身份"
        ),
    )
    automation = account.get("automation") or {}
    if bool(automation.get("paused")):
        reason = str(
            automation.get("reason") or "自动动作已暂停，请在账号管理中恢复"
        )[:300]
        for key in ("publish", "verification", "comments", "dm"):
            capabilities[key] = _item(False, "blocked", reason)
    return capabilities
