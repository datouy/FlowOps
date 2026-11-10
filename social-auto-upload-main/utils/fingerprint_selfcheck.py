# -*- coding: utf-8 -*-
"""动态指纹自检（fail-closed）。

静态的 fingerprint_consistency 只能查出“指纹数据自己打架”，但查不出
“注入没生效”。这里在账号浏览器启动后打开一个空白页，读回 navigator/screen
上实际暴露的信号，和账号记录的指纹逐一比对。关键字段不一致就抛
FingerprintMismatchError，拒绝继续执行发布 / 私信 / 巡检动作。

原则：只把“我们确定性注入、且读得准”的字段列为硬性失败；WebGL 与 UA-CH
高熵这类受内核/驱动影响的字段只做软提示，避免误杀真实账号。
"""
from __future__ import annotations

from typing import Any


SELFCHECK_SCRIPT = r"""
(() => {
  let uadb = null;
  try {
    if (navigator.userAgentData) {
      uadb = {
        brands: Array.from(navigator.userAgentData.brands || []).map(
          (b) => ({ brand: b.brand, version: b.version })
        ),
        platform: navigator.userAgentData.platform,
      };
    }
  } catch (_) {}
  return {
    ua: navigator.userAgent,
    platform: navigator.platform,
    hc: navigator.hardwareConcurrency,
    dm: navigator.deviceMemory,
    lang: navigator.language,
    langs: (navigator.languages ? Array.from(navigator.languages) : []),
    webdriver: navigator.webdriver,
    sw: screen.width,
    sh: screen.height,
    sah: screen.availHeight,
    cd: screen.colorDepth,
    uadb: uadb,
  };
})()
"""


_PLATFORM_HINTS = {
    "windows": ("Windows NT", "Windows"),
    "macos": ("Macintosh", "Mac OS X"),
    "linux": ("X11; Linux", "Linux"),
}


def _chrome_major(ua: str) -> str:
    import re
    match = re.search(r"Chrome/([0-9]+)", str(ua or ""))
    return match.group(1) if match else ""


def compare(result: dict, fingerprint: dict):
    hard = []
    soft = []
    fp = fingerprint or {}

    if result.get("webdriver") is True:
        hard.append("navigator.webdriver=true（自动化特征未隐藏）")

    platform_name = str(fp.get("platform_name") or "")
    if platform_name:
        actual_platform = str(result.get("platform") or "")
        if actual_platform and actual_platform != platform_name:
            hard.append("navigator.platform=" + repr(actual_platform) + "，期望 " + repr(platform_name))

    hc = fp.get("hardware_concurrency")
    if hc is not None and result.get("hc") is not None and int(result["hc"]) != int(hc):
        hard.append("hardwareConcurrency=" + str(result["hc"]) + "，期望 " + str(hc))

    dm = fp.get("device_memory")
    if dm is not None and result.get("dm") is not None and int(result["dm"]) != int(dm):
        hard.append("deviceMemory=" + str(result["dm"]) + "，期望 " + str(dm))

    langs = fp.get("languages") or []
    if langs:
        first = str(langs[0])
        actual_lang = str(result.get("lang") or "")
        actual_langs = [str(x) for x in (result.get("langs") or [])]
        if actual_lang and actual_lang != first:
            hard.append("navigator.language=" + repr(actual_lang) + "，期望 " + repr(first))
        elif actual_langs and first not in actual_langs:
            hard.append("navigator.languages=" + repr(actual_langs) + "，缺少 " + repr(first))

    screen = fp.get("screen") or {}
    for key, expected in (("sw", "width"), ("sh", "height"), ("sah", "avail_height")):
        want = int(screen.get(expected) or 0)
        got = result.get(key)
        if want and got is not None and int(got) != want:
            hard.append("screen." + expected + "=" + str(got) + "，期望 " + str(want))

    major = str(fp.get("chrome_major") or _chrome_major(str(fp.get("user_agent") or "")))
    ua = str(result.get("ua") or "")
    if major and major not in ua:
        hard.append("navigator.userAgent 未声明 Chrome " + major)
    platform_key = str(fp.get("platform") or "")
    hints = _PLATFORM_HINTS.get(platform_key) or ()
    if hints and ua and not any(hint in ua for hint in hints):
        hard.append("userAgent 与 platform=" + platform_key + " 不匹配")

    uadb = result.get("uadb")
    data = fp.get("user_agent_data") or {}
    if isinstance(uadb, dict) and isinstance(data, dict):
        expected_brands = {}
        for b in (data.get("brands") or []):
            if isinstance(b, dict):
                expected_brands[str(b.get("brand") or "")] = str(b.get("version") or "")
        actual_brands = {}
        for b in (uadb.get("brands") or []):
            if isinstance(b, dict):
                actual_brands[str(b.get("brand") or "")] = str(b.get("version") or "")
        chrome_ver = expected_brands.get("Google Chrome") or expected_brands.get("Chromium")
        if chrome_ver:
            got = actual_brands.get("Google Chrome") or actual_brands.get("Chromium")
            if got and got != chrome_ver:
                soft.append("UA-CH Chrome 版本 " + got + "，期望 " + chrome_ver)

    return hard, soft


def hard_failures(result: dict, fingerprint: dict):
    return compare(result, fingerprint)[0]


async def assert_fingerprint(context, fingerprint):
    from utils.errors import FingerprintMismatchError

    page = await context.new_page()
    try:
        result = await page.evaluate(SELFCHECK_SCRIPT)
    finally:
        await page.close()
    hard, soft = compare(result or {}, fingerprint)
    if hard:
        raise FingerprintMismatchError("；".join(hard))
    return soft


def assert_fingerprint_sync(context, fingerprint):
    from utils.errors import FingerprintMismatchError

    page = context.new_page()
    try:
        result = page.evaluate(SELFCHECK_SCRIPT)
    finally:
        page.close()
    hard, soft = compare(result or {}, fingerprint)
    if hard:
        raise FingerprintMismatchError("；".join(hard))
    return soft


async def verify_fingerprint(context, fingerprint):
    """诊断用：不抛异常，返回 (hard, soft, 实测值)。"""
    page = await context.new_page()
    try:
        result = await page.evaluate(SELFCHECK_SCRIPT)
    finally:
        await page.close()
    hard, soft = compare(result or {}, fingerprint)
    return hard, soft, result


def verify_fingerprint_sync(context, fingerprint):
    """同步诊断版，返回 (hard, soft, 实测值)。"""
    page = context.new_page()
    try:
        result = page.evaluate(SELFCHECK_SCRIPT)
    finally:
        page.close()
    hard, soft = compare(result or {}, fingerprint)
    return hard, soft, result
