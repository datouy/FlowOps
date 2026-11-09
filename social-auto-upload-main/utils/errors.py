# -*- coding: utf-8 -*-
"""平台异常的层次结构。

**为什么不用中文关键词匹配**：现在的实现靠一堆中文子串去猜异常类型
（``'登录'`` / ``'风控'`` / ``'验证码'``）。这有三个硬伤：

1. **平台改文案就漏判**。平台把"请重新登录"改成"登录状态已过期"，
   规则就匹配不上了，异常会被降级成 ``unknown``，然后照常重试——
   而登录失效恰恰是**绝对不能重试**的，重试等于二次触发风控。
2. **关键词会误伤**。正文里出现"登录"两个字的普通错误（"素材下载失败，
   请检查网络后重新登录"）会被判成登录失效，账号被无谓地标记为异常。
3. **重试策略与错误类型脱钩**。现在是"失败就重试 1 次"一刀切，网络抖动
   该重试，账号被封不该重试，参数写错重试一万次也没用。

**这里的设计**：

* 平台适配层抛**带类型的异常**（``AuthExpiredError`` / ``RiskControlError``
  / ``TransientError`` …），类型在**抛出点**就确定，不靠事后猜字符串。
* 每种异常自带两个决策属性：
  - ``retryable``：该不该重试；
  - ``account_effect``：对账号状态做什么（标记失效 / 降速 / 无影响）。
* 老代码抛的裸 ``Exception`` 走 ``classify_publish_error`` 兜底——那是
  **过渡期兼容层**，不是主路径。新代码应该直接抛这里的类型。

这样"重试策略"从一个 ``if`` 变成异常类型自带的属性，平台适配器升级时
只需要改抛出点，不需要改重试逻辑。
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------- 账号影响

# 对账号状态的影响。
ACCOUNT_NONE = "none"             # 不影响账号
ACCOUNT_EXPIRE = "expire"         # 登录态失效，账号应标记为异常
ACCOUNT_THROTTLE = "throttle"     # 被风控，账号应降速一段时间


# ---------------------------------------------------------------- 异常基类

class AppError(Exception):
    """所有平台异常的基类。

    带一个稳定的 ``code``（用于落库与前端展示，不随文案变化）。
    """

    #: 落库用的稳定错误码
    code = "app_error"
    #: 是否值得重试
    retryable = False
    #: 对账号状态的影响（ACCOUNT_* 之一）
    account_effect = ACCOUNT_NONE
    #: 建议的退避基数（秒）；``retryable`` 为 True 时有意义
    backoff_base = 5.0
    #: 最大重试次数
    max_attempts = 3

    def __init__(self, message: str = "", **context: Any):
        super().__init__(message)
        self.message = str(message or "")
        self.context = context

    def __str__(self) -> str:
        return self.message or self.__class__.__name__


class TransientError(AppError):
    """暂时性问题：网络抖动、上传中断、平台 5xx。

    这类错误**值得重试**——重试的是"环境"，不是"账号"。
    """

    code = "transient"
    retryable = True
    account_effect = ACCOUNT_NONE
    backoff_base = 5.0
    max_attempts = 3


class AuthExpiredError(AppError):
    """登录态失效（302 / 跳登录页 / 拿不到用户信息）。

    **绝不重试**：重试只会让平台更确信这个账号在撞风控。应该立刻把账号
    标记为失效，通知人工重新扫码。
    """

    code = "login_expired"
    retryable = False
    account_effect = ACCOUNT_EXPIRE


class RiskControlError(AppError):
    """触发平台风控（验证码、频控、环境异常）。

    不重试，但**要降速**：短时间内这个账号不该再被操作。
    """

    code = "risk_control"
    retryable = False
    account_effect = ACCOUNT_THROTTLE
    backoff_base = 1800.0


class PlatformRejectError(AppError):
    """平台明确拒绝：内容违规、参数非法、发布频率限制。

    不重试。内容有问题时重试一万次也一样被拒。
    """

    code = "platform_reject"
    retryable = False
    account_effect = ACCOUNT_NONE


class SelectorDriftError(AppError):
    """选择器失效——平台改版了。

    不重试（重试还是找不到），但要**告警**：这是需要人改代码的信号，
    不是账号的问题。
    """

    code = "selector_drift"
    retryable = False
    account_effect = ACCOUNT_NONE


class ResourceMissingError(AppError):
    """素材或文件不存在。"""

    code = "file_missing"
    retryable = False
    account_effect = ACCOUNT_NONE


class UnknownPlatformError(AppError):
    """平台不支持该操作。"""

    code = "unsupported_platform"
    retryable = False
    account_effect = ACCOUNT_NONE


# ---------------------------------------------------------------- 工具

#: 错误码 → 异常类。新写的异常在这里登记。
ERROR_CODES: dict[str, type[AppError]] = {
    TransientError.code: TransientError,
    AuthExpiredError.code: AuthExpiredError,
    RiskControlError.code: RiskControlError,
    PlatformRejectError.code: PlatformRejectError,
    SelectorDriftError.code: SelectorDriftError,
    ResourceMissingError.code: ResourceMissingError,
    UnknownPlatformError.code: UnknownPlatformError,
}


def error_code_for(exc: BaseException) -> str:
    """取一个异常的错误码。

    已经是 ``AppError`` 就用它的 ``code``；否则回落到已有的
    ``error_kind`` 分类结果（兼容层），最后兜底成 ``unknown``。
    """
    if isinstance(exc, AppError):
        return exc.code
    kind = getattr(exc, "error_kind", "")
    if kind:
        return str(kind)
    try:
        from publish_jobs import classify_publish_error

        return classify_publish_error(str(exc))
    except Exception:
        return "unknown"


def is_retryable(exc: BaseException) -> bool:
    """这个异常该不该重试。

    只认**类型**上的判断，不看文案——这是本模块存在的意义。
    """
    if isinstance(exc, AppError):
        return exc.retryable
    # 兼容层：老代码抛的裸异常，靠分类结果判断。
    try:
        from publish_jobs import classify_publish_error

        return classify_publish_error(str(exc)) == "network"
    except Exception:
        return False


def account_effect(exc: BaseException) -> str:
    """这个异常对账号状态的影响（ACCOUNT_* 之一）。"""
    if isinstance(exc, AppError):
        return exc.account_effect
    try:
        from publish_jobs import classify_publish_error

        kind = classify_publish_error(str(exc))
    except Exception:
        return ACCOUNT_NONE
    if kind == "login_expired":
        return ACCOUNT_EXPIRE
    if kind == "risk_control":
        return ACCOUNT_THROTTLE
    return ACCOUNT_NONE


def backoff_seconds(exc: BaseException, attempt: int) -> float:
    """指数退避时长（秒），带抖动。

    ``attempt`` 从 1 开始。抖动是必要的：多个账号同时失败时，没有抖动
    它们会在同一毫秒一起重试，等于把一次故障放大成一场。
    """
    import random

    if isinstance(exc, AppError):
        base = exc.backoff_base
    else:
        base = 5.0
    exponent = max(0, int(attempt) - 1)
    delay = base * (2 ** exponent)
    # 最多 30% 抖动。
    jitter = delay * 0.3 * random.random()
    return min(delay + jitter, 1800.0)


def max_attempts_for(exc: BaseException) -> int:
    """这个异常最多重试几次。"""
    if isinstance(exc, AppError):
        return max(1, int(exc.max_attempts))
    return 1


def describe(exc: BaseException) -> dict[str, Any]:
    """给日志与事件流用的结构化描述。"""
    return {
        "code": error_code_for(exc),
        "message": str(exc)[:500],
        "retryable": is_retryable(exc),
        "accountEffect": account_effect(exc),
        "type": type(exc).__name__,
    }
