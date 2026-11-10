# -*- coding: utf-8 -*-
"""Stable browser fingerprints for account-isolated platform sessions.

The storage-state JSON is Playwright's portable login bundle, but WeChat
Channels also binds a web session to browser signals. Replaying the same
bundle under a different UA can make a valid account look expired. This
module keeps those signals beside the credential file and gives each newly
onboarded account its own stable profile.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


FINGERPRINT_DIR_NAME = ".fingerprints"
# P0-2：UA 主版本必须与 Playwright Chromium 对齐。
# 实测本机 chromium-1248 对应 156；不再写死 131，允许环境覆盖，
# 新建指纹默认取运行期版本，老指纹保持不动（换 UA 等于换设备）。
def chromium_major_default() -> str:
    for env in ("FLOWOPS_CHROME_MAJOR", "FLOWOPS_BROWSER_MAJOR"):
        try:
            v = str(__import__("os").environ.get(env) or "").strip().split(".", 1)[0]
            if v.isdigit():
                return v
        except Exception:
            pass
    return "156"
_CHROME_MAJOR_DEFAULT = chromium_major_default()
_UA_PROFILES = (
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
        "windows",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
        "macos",
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
        "linux",
    ),
)
_CHROME_MAJORS = (154, 155, 156, 157)
_PLATFORM_NAMES = {
    "windows": "Win32",
    "macos": "MacIntel",
    "linux": "Linux x86_64",
}
_SCREEN_PROFILES = (
    {"width": 1920, "height": 1080, "avail_height": 1040},
    {"width": 1536, "height": 864, "avail_height": 824},
    {"width": 1440, "height": 900, "avail_height": 860},
)


def _digest_int(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:12], 16)


def legacy_linux_fingerprint(browser_version: str = _CHROME_MAJOR_DEFAULT) -> dict[str, Any]:
    """Fingerprint used by builds before account-level fingerprints existed."""
    major = str(browser_version or _CHROME_MAJOR_DEFAULT).split(".", 1)[0]
    return _normalize_fingerprint({
        "version": 1,
        "id": "legacy-linux",
        "platform": "linux",
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        ),
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "viewport": {"width": 1280, "height": 900},
        "device_scale_factor": 1,
        "color_scheme": "light",
    }, "legacy-linux")


def new_fingerprint(seed: str, browser_version: str = _CHROME_MAJOR_DEFAULT) -> dict[str, Any]:
    """Create a deterministic profile for one account login environment."""
    # P0-2：UA 主版本必须与 Chromium 对齐 —— 不再随机挑 major，
    # 统一取 browser_version 的主版本，避免 UA 132 / 内核 131 的穿帮。
    # Seed 只决定 platform（windows/macos/linux），不决定版本。
    numeric = re.search(r"(\d+)$", seed)
    number = int(numeric.group(1)) if numeric else _digest_int(seed)
    template, platform = _UA_PROFILES[number % len(_UA_PROFILES)]
    major = str(str(browser_version or _CHROME_MAJOR_DEFAULT).split(".", 1)[0] or _CHROME_MAJOR_DEFAULT)
    return _normalize_fingerprint({
        "version": 1,
        "id": hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20],
        "platform": platform,
        "user_agent": template.format(major=major),
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "viewport": {"width": 1280, "height": 900},
        "device_scale_factor": 1,
        "color_scheme": "light",
    }, seed)


def _normalize_fingerprint(
    raw: dict[str, Any], seed: str | None = None
) -> dict[str, Any]:
    """Fill fields introduced after the first fingerprint format."""
    payload = dict(raw)
    payload["version"] = max(2, int(payload.get("version") or 1))
    basis = seed or str(payload.get("id") or "fingerprint")
    number = _digest_int(basis)
    platform = str(payload.get("platform") or "linux")
    screen = _SCREEN_PROFILES[number % len(_SCREEN_PROFILES)]
    payload.setdefault("platform_name", _PLATFORM_NAMES.get(platform, "Linux x86_64"))
    payload.setdefault("device_id", hashlib.sha256((basis + ":device").encode("utf-8")).hexdigest()[:32])
    payload.setdefault("hardware_concurrency", (4, 8, 12, 16)[number % 4])
    payload.setdefault("device_memory", (8, 12, 16)[number % 3])
    payload.setdefault("screen", screen)
    payload.setdefault("languages", ["zh-CN", "zh", "en"])
    ua_match = re.search(r"Chrome/(\d+)", str(payload.get("user_agent") or ""))
    chrome_major = ua_match.group(1) if ua_match else _CHROME_MAJOR_DEFAULT
    payload.setdefault("chrome_major", chrome_major)
    payload.setdefault(
        "user_agent_data",
        {
            "brands": [
                {"brand": "Not_A Brand", "version": "24"},
                {"brand": "Chromium", "version": chrome_major},
                {"brand": "Google Chrome", "version": chrome_major},
            ],
            "fullVersionList": [
                {"brand": "Not_A Brand", "version": "24.0.0.0"},
                {"brand": "Chromium", "version": f"{chrome_major}.0.0.0"},
                {"brand": "Google Chrome", "version": f"{chrome_major}.0.0.0"},
            ],
            "platform": (
                "Windows" if platform == "windows"
                else "macOS" if platform == "macos"
                else "Linux"
            ),
            "platformVersion": (
                "15.0.0" if platform == "windows"
                else "14.6.1" if platform == "macos"
                else "6.8.0"
            ),
            "architecture": "x86",
            "bitness": "64",
            "model": "",
            "wow64": False,
        },
    )
    payload.setdefault(
        "webgl_vendor",
        {
            "windows": "Google Inc. (Intel)",
            "macos": "Google Inc. (Apple)",
            "linux": "Google Inc. (Intel)",
        }.get(platform, "Google Inc. (Intel)"),
    )
    payload.setdefault(
        "webgl_renderer",
        {
            "windows": "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)",
            "macos": "ANGLE (Apple, Apple M2, OpenGL 4.1)",
            "linux": "ANGLE (Intel, Mesa Intel(R) UHD Graphics, OpenGL 4.5)",
        }.get(platform, "ANGLE (Intel, Intel(R) UHD Graphics, OpenGL 4.5)"),
    )
    return payload


def upgrade_fingerprint(fingerprint: dict[str, Any], seed: str) -> dict[str, Any]:
    """Add account-scoped fields while keeping the original UA/platform."""
    return _normalize_fingerprint(fingerprint, seed)


def fingerprint_path(cookie_path: str | Path) -> Path:
    path = Path(cookie_path)
    return path.parent / FINGERPRINT_DIR_NAME / f"{path.name}.json"


def load_fingerprint(cookie_path: str | Path) -> dict[str, Any] | None:
    path = fingerprint_path(cookie_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload.get("user_agent"):
        return None
    return payload


def save_fingerprint(cookie_path: str | Path, fingerprint: dict[str, Any]) -> None:
    path = fingerprint_path(cookie_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(fingerprint, ensure_ascii=False, indent=2)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def context_options(fingerprint: dict[str, Any] | None) -> dict[str, Any]:
    """Translate a stored fingerprint into Playwright context arguments."""
    if not fingerprint:
        return {}
    viewport = fingerprint.get("viewport") or {}
    width = int(viewport.get("width") or 1280)
    height = int(viewport.get("height") or 900)
    return {
        "user_agent": str(fingerprint["user_agent"]),
        "locale": str(fingerprint.get("locale") or "zh-CN"),
        "timezone_id": str(fingerprint.get("timezone_id") or "Asia/Shanghai"),
        "viewport": {"width": width, "height": height},
        "device_scale_factor": float(fingerprint.get("device_scale_factor") or 1),
        "color_scheme": str(fingerprint.get("color_scheme") or "light"),
    }


def fingerprint_for_storage(cookie_path: str | Path) -> dict[str, Any] | None:
    """Replay a saved profile without changing its existing device identity."""
    fingerprint = load_fingerprint(cookie_path)
    try:
        payload = json.loads(Path(cookie_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    stored_device_id = ""
    for origin in payload.get("origins", []):
        for item in origin.get("localStorage", []):
            if item.get("name") == "_finger_print_device_id":
                stored_device_id = str(item.get("value") or "").strip()
                break
        if stored_device_id:
            break
    if stored_device_id:
        if fingerprint is None:
            fingerprint = legacy_linux_fingerprint(_CHROME_MAJOR_DEFAULT)
        fingerprint = _normalize_fingerprint(fingerprint)
        fingerprint["device_id"] = stored_device_id
        return fingerprint
    if fingerprint is None:
        return None
    fingerprint = _normalize_fingerprint(fingerprint)
    return fingerprint


def fingerprint_init_script(fingerprint: dict[str, Any] | None) -> str:
    """Install stable device, screen and WebGL signals before site scripts.

    P0-2 加固版：canvas/audio/font 低位噪声（seed= device_id，确定性、
    只动不透明像素、导出走副本），webdriver 删除（不是置 false），
    Function.prototype.toString 透传避免“一探即知”。
    """
    profile = _normalize_fingerprint(fingerprint or {})
    screen = profile.get("screen") or _SCREEN_PROFILES[0]
    device_id = str(profile.get("device_id") or "yunce")
    values = {
        "platform": profile.get("platform_name"),
        "hardwareConcurrency": profile.get("hardware_concurrency"),
        "deviceMemory": profile.get("device_memory"),
        "languages": profile.get("languages"),
        "userAgentData": profile.get("user_agent_data"),
    }
    return f"""
(() => {{
  const values = {json.dumps(values, ensure_ascii=False)};
  const seedStr = {json.dumps(device_id, ensure_ascii=False)};
  let seed = 0;
  for (let i = 0; i < seedStr.length; i++) {{ seed = (seed * 31 + seedStr.charCodeAt(i)) >>> 0; }}
  const rand = () => {{ seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }};
  const define = (target, key, value) => {{
    try {{ Object.defineProperty(target, key, {{get: () => value, configurable: true}}); }} catch (_) {{}}
  }};
  const hideFn = (fn) => {{
    try {{
      const src = Function.prototype.toString.call(fn);
      fn.toString = () => src;
    }} catch (_) {{}}
    return fn;
  }};
  define(Navigator.prototype, 'platform', values.platform);
  define(Navigator.prototype, 'hardwareConcurrency', values.hardwareConcurrency);
  define(Navigator.prototype, 'deviceMemory', values.deviceMemory);
  define(Navigator.prototype, 'languages', values.languages);
  define(Navigator.prototype, 'language', values.languages[0]);
  try {{ delete Navigator.prototype.webdriver; }} catch (_) {{}}
  try {{ delete navigator.webdriver; }} catch (_) {{}}
  define(Navigator.prototype, 'webdriver', undefined);
  if (values.userAgentData) {{
    const data = values.userAgentData;
    define(Navigator.prototype, 'userAgentData', {{
      ...data,
      getHighEntropyValues: hideFn(async (hints) => {{
        const result = {{
          brands: data.brands,
          mobile: false,
          platform: data.platform,
          fullVersionList: data.fullVersionList,
          platformVersion: data.platformVersion,
          architecture: data.architecture,
          bitness: data.bitness,
          model: data.model,
          wow64: data.wow64,
        }};
        return Object.fromEntries((hints || []).filter(k => k in result).map(k => [k, result[k]]));
      }}),
      toJSON: () => ({{brands: data.brands, mobile: false, platform: data.platform}}),
    }});
  }}
  const screenWidth = {int(screen['width'])};
  const screenHeight = {int(screen['height'])};
  const availHeight = {int(screen['avail_height'])};
  define(Screen.prototype, 'width', screenWidth);
  define(Screen.prototype, 'height', screenHeight);
  define(Screen.prototype, 'availWidth', screenWidth);
  define(Screen.prototype, 'availHeight', availHeight);
  define(Screen.prototype, 'colorDepth', 24);
  define(Screen.prototype, 'pixelDepth', 24);
  const patchGl = (proto) => {{
    if (!proto) return;
    const original = proto.getParameter;
    const patched = function(parameter) {{
      if (parameter === 37445) return {json.dumps(str(profile.get('webgl_vendor')), ensure_ascii=False)};
      if (parameter === 37446) return {json.dumps(str(profile.get('webgl_renderer')), ensure_ascii=False)};
      return original.call(this, parameter);
    }};
    hideFn(patched);
    proto.getParameter = patched;
  }};
  patchGl(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGl(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  // canvas 低位噪声：只动不透明像素最低位，肉眼不可见、哈希稳定偏移
  try {{
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    const patchedGet = function(sx, sy, sw, sh, ...rest) {{
      const img = origGetImageData.call(this, sx, sy, sw, sh, ...rest);
      try {{
        const d = img.data;
        for (let i = 0; i < d.length; i += 4) {{
          if (d[i + 3] === 0) continue;
          const n = Math.floor(rand() * 3) - 1;
          d[i] = Math.max(0, Math.min(255, d[i] + n));
        }}
      }} catch (_) {{}}
      return img;
    }};
    hideFn(patchedGet);
    CanvasRenderingContext2D.prototype.getImageData = patchedGet;
  }} catch (_) {{}}
  // audio：OfflineAudioContext 1e-8 级扰动
  try {{
    const OrigAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (OrigAC && !OrigAC.__yuncePatched) {{
      const Patched = function(...args) {{
        const ctx = new OrigAC(...args);
        try {{
          const origStart = ctx.startRendering.bind(ctx);
          ctx.startRendering = () => origStart().then((buf) => {{
            try {{
              const ch = buf.getChannelData(0);
              for (let i = 0; i < Math.min(ch.length, 4096); i += 997) ch[i] += (rand() - 0.5) * 1e-7;
            }} catch (_) {{}}
            return buf;
          }});
        }} catch (_) {{}}
        return ctx;
      }};
      Patched.prototype = OrigAC.prototype;
      window.OfflineAudioContext = Patched;
      window.OfflineAudioContext.__yuncePatched = true;
    }}
  }} catch (_) {{}}
  // font：measureText 系数抖动
  try {{
    const origMeasure = CanvasRenderingContext2D.prototype.measureText;
    const patchedMeasure = function(text) {{
      const m = origMeasure.call(this, text);
      try {{
        const jitter = 1 + (rand() - 0.5) * 0.0004;
        Object.defineProperty(m, 'width', {{ value: m.width * jitter, configurable: true }});
      }} catch (_) {{}}
      return m;
    }};
    hideFn(patchedMeasure);
    CanvasRenderingContext2D.prototype.measureText = patchedMeasure;
  }} catch (_) {{}}
  // WebRTC fail-closed：只允许 relay（经代理），禁止 host/srflx 直连透真实 IP；
  // enumerateDevices 隐藏 device label，避免硬件指纹直读。
  try {{
    const OrigRTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (OrigRTC && !OrigRTC.__yuncePatched) {{
      const PatchedRTC = function(...args) {{
        let cfg = args[0] || {{}};
        cfg = Object.assign({{}}, cfg, {{ iceTransportPolicy: 'relay' }});
        if (!cfg.iceServers || !cfg.iceServers.length) {{
          cfg.iceServers = [{{ urls: 'stun:127.0.0.1:19302' }}];
        }}
        return new OrigRTC(cfg);
      }};
      PatchedRTC.prototype = OrigRTC.prototype;
      PatchedRTC.__yuncePatched = true;
      try {{ window.RTCPeerConnection = PatchedRTC; }} catch (_) {{}}
      try {{ window.webkitRTCPeerConnection = PatchedRTC; }} catch (_) {{}}
    }}
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {{
      const origEnum = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
      const patchedEnum = hideFn(async () => {{
        const list = await origEnum();
        return list.map((d) => ({{ kind: d.kind, deviceId: '', groupId: '' }}));
      }});
      try {{ Object.defineProperty(navigator.mediaDevices, 'enumerateDevices', {{ value: patchedEnum, configurable: true }}); }} catch (_) {{}}
    }}
  }} catch (_) {{}}
  try {{ localStorage.setItem('_finger_print_device_id', {json.dumps(str(profile.get('device_id')), ensure_ascii=False)}); }} catch (_) {{}}
}})();
"""


# ---------------------------------------------------------------- 一致性自检
#
# 伪造/复用的指纹最容易在"同一份数据自相矛盾"上露馅，而且这种矛盾**不需要
# 启动浏览器**就能查出来：UA 说 Chrome 132、UA-CH 的 brands 说 131；platform
# 写 Win32 但 UA 是 Macintosh；屏幕 1920x1080 但 availHeight 比 height 还大。
# 平台侧任何一次高熵取值都会把这些矛盾拿走，所以发布前先自己查一遍。

_UA_PLATFORM_HINTS = (
    ("windows", ("Windows NT",)),
    ("macos", ("Macintosh", "Mac OS X")),
    ("linux", ("X11; Linux", "Linux x86_64")),
)


def user_agent_major(user_agent: str) -> str:
    """从 UA 里取 Chrome 主版本号，取不到返回空串。"""
    match = re.search(r"Chrome/(\d+)", str(user_agent or ""))
    return match.group(1) if match else ""


def fingerprint_consistency(profile: dict[str, Any] | None) -> list[str]:
    """返回指纹内部不一致的描述列表（空列表 = 自洽）。

    只做静态比对，不发请求、不起浏览器，可以在预检里随便调。
    """
    if not isinstance(profile, dict) or not profile:
        return ["指纹为空"]
    problems: list[str] = []
    ua = str(profile.get("user_agent") or "")
    if not ua:
        return ["指纹里没有 user_agent"]

    major = user_agent_major(ua)
    if not major:
        problems.append("UA 里没有 Chrome 版本号")

    data = profile.get("user_agent_data")
    if isinstance(data, dict):
        brands = data.get("brands") or []
        for brand in brands:
            if not isinstance(brand, dict):
                continue
            name = str(brand.get("brand") or "")
            version = str(brand.get("version") or "")
            if name == "Google Chrome" and major and version and version != major:
                problems.append(
                    f"UA 声明 Chrome {major}，UA-CH brands 声明 {version}"
                )
        full = data.get("fullVersionList") or []
        if isinstance(full, list) and full:
            first = full[0] if isinstance(full[0], dict) else {}
            if not first.get("brand"):
                problems.append("UA-CH fullVersionList 结构异常")
    else:
        problems.append("指纹里没有 user_agent_data（高熵取值会直接暴露缺失）")

    platform_key = str(profile.get("platform") or "").strip().lower()
    if platform_key:
        hints = dict(_UA_PLATFORM_HINTS).get(platform_key) or ()
        if hints and not any(hint in ua for hint in hints):
            problems.append(f"platform={platform_key} 与 UA 里的系统标识不一致")

    screen = profile.get("screen") or {}
    height = int(screen.get("height") or 0)
    avail = int(screen.get("avail_height") or 0)
    if height and avail and avail > height:
        problems.append(f"屏幕可用高度 {avail} 大于总高度 {height}")

    viewport = profile.get("viewport") or {}
    vw = int(viewport.get("width") or 0)
    if vw and screen.get("width") and vw > int(screen["width"]):
        problems.append("视口宽度大于屏幕宽度")

    languages = profile.get("languages")
    if not languages:
        problems.append("指纹里没有 languages")
    return problems
