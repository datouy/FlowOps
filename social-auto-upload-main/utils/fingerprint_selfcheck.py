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
  let canvasHash = "";
  try {
    const c = document.createElement('canvas');
    c.width = 200; c.height = 50;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#f60'; ctx.fillRect(0, 0, 200, 50);
    ctx.fillStyle = '#069'; ctx.font = '16px Arial';
    ctx.fillText('yunce-probe-云策', 10, 30);
    canvasHash = c.toDataURL().slice(-64);
  } catch (_) {}
  let audioHash = "";
  try {
    const AC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    audioHash = AC ? "available" : "missing";
  } catch (_) { audioHash = "error"; }
  let fontProbe = [];
  try {
    const base = document.createElement('canvas').getContext('2d');
    base.font = '16px monospace';
    const w0 = base.measureText('mmmmmmmmmm').width;
    for (const f of ['Arial', 'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK']) {
      base.font = `16px "${f}", monospace`;
      const w = base.measureText('mmmmmmmmmm').width;
      fontProbe.push({font: f, diff: Math.abs(w - w0) > 0.01});
    }
  } catch (_) {}
  let webdriverDesc = "";
  try {
    const d = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
    webdriverDesc = d ? (typeof d.get === 'function' ? 'getter' : 'value:' + String(d.value)) : 'absent';
  } catch (_) { webdriverDesc = 'error'; }
  let webdriverInProto = false;
  try { webdriverInProto = 'webdriver' in Navigator.prototype; } catch (_) {}
  let tz = "";
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (_) {}
  return {
    ua: navigator.userAgent,
    platform: navigator.platform,
    hc: navigator.hardwareConcurrency,
    dm: navigator.deviceMemory,
    lang: navigator.language,
    langs: (navigator.languages ? Array.from(navigator.languages) : []),
    webdriver: navigator.webdriver,
    webdriverDesc: webdriverDesc,
    webdriverInProto: webdriverInProto,
    sw: screen.width,
    sh: screen.height,
    sah: screen.availHeight,
    cd: screen.colorDepth,
    tz: tz,
    uadb: uadb,
    canvasHash: canvasHash,
    audioHash: audioHash,
    fontProbe: fontProbe,
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

    # 1. webdriver 必须删掉：值为 True 或原型上仍存在属性都算暴露
    if result.get("webdriver") is True:
        hard.append("navigator.webdriver=true（自动化特征未隐藏）")
    if result.get("webdriverInProto") is True:
        hard.append("Navigator.prototype 仍存在 webdriver 属性（删除未生效）")
    desc = str(result.get("webdriverDesc") or "")
    if desc.startswith("value:"):
        hard.append("navigator.webdriver 可被直接读取（应为 undefined/不可枚举）")

    # 2. platform
    platform_name = str(fp.get("platform_name") or "")
    if platform_name:
        actual_platform = str(result.get("platform") or "")
        if actual_platform and actual_platform != platform_name:
            hard.append("navigator.platform=" + repr(actual_platform) + "，期望 " + repr(platform_name))

    # 3-4. 硬件并发 / 内存
    hc = fp.get("hardware_concurrency")
    if hc is not None and result.get("hc") is not None and int(result["hc"]) != int(hc):
        hard.append("hardwareConcurrency=" + str(result["hc"]) + "，期望 " + str(hc))

    dm = fp.get("device_memory")
    if dm is not None and result.get("dm") is not None and int(result["dm"]) != int(dm):
        hard.append("deviceMemory=" + str(result["dm"]) + "，期望 " + str(dm))

    # 5. 语言
    langs = fp.get("languages") or []
    if langs:
        first = str(langs[0])
        actual_lang = str(result.get("lang") or "")
        actual_langs = [str(x) for x in (result.get("langs") or [])]
        if actual_lang and actual_lang != first:
            hard.append("navigator.language=" + repr(actual_lang) + "，期望 " + repr(first))
        elif actual_langs and first not in actual_langs:
            hard.append("navigator.languages=" + repr(actual_langs) + "，缺少 " + repr(first))

    # 6-8. 屏幕三件套 + 色深
    screen = fp.get("screen") or {}
    for key, expected in (("sw", "width"), ("sh", "height"), ("sah", "avail_height")):
        want = int(screen.get(expected) or 0)
        got = result.get(key)
        if want and got is not None and int(got) != want:
            hard.append("screen." + expected + "=" + str(got) + "，期望 " + str(want))
    if result.get("cd") is not None and int(result.get("cd") or 0) != 24:
        hard.append("screen.colorDepth=" + str(result.get("cd")) + "，期望 24")

    # 9. 时区必须与指纹一致（默认 Asia/Shanghai）
    tz = str(result.get("tz") or "")
    if tz and tz != "Asia/Shanghai":
        hard.append("时区=" + repr(tz) + "，期望 'Asia/Shanghai'")

    # 10. UA 主版本
    major = str(fp.get("chrome_major") or _chrome_major(str(fp.get("user_agent") or "")))
    ua = str(result.get("ua") or "")
    if major and major not in ua:
        hard.append("navigator.userAgent 未声明 Chrome " + major)
    platform_key = str(fp.get("platform") or "")
    hints = _PLATFORM_HINTS.get(platform_key) or ()
    if hints and ua and not any(hint in ua for hint in hints):
        hard.append("userAgent 与 platform=" + platform_key + " 不匹配")

    # 11. UA-CH 主版本必须与 UA 对齐（fail-closed：不对齐直接硬失败）
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
                hard.append("UA-CH Chrome 版本 " + got + "，期望 " + chrome_ver)

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
