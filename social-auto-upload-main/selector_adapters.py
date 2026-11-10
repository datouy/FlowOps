# -*- coding: utf-8 -*-
"""选择器远端适配包（P0-4）：adapters/<platform>.yaml 有序候选 + 断言。

- 本地回退：adapters/*.yaml 不存在则用内置默认候选
- 远端签名拉取：FLOWOPS_ADAPTER_URL + FLOWOPS_ADAPTER_KEY，HMAC 校验后覆盖本地
- SelectorDrift：找不到时自动打包 URL/选择器/DOM/截图，一键导出
"""
from __future__ import annotations

import hashlib
import hmac as hmac_lib
import json
import os
import time
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
ADAPTER_DIR = BASE / "adapters"

BUILTIN = {
    "douyin": [
        {"selector": ".semi-list-item", "assert": "visible", "timeout": 12000},
        {"selector": "[class*='list-item']", "assert": "attached", "timeout": 8000},
    ],
    "xiaohongshu": [
        {"selector": ".conversation-item", "assert": "visible", "timeout": 12000},
        {"selector": "[class*='conversation']", "assert": "attached", "timeout": 8000},
    ],
    "channels": [
        {"selector": "[class*='chat-item']", "assert": "visible", "timeout": 12000},
    ],
}


def _parse_simple_yaml(text: str) -> list[dict]:
    items: list[dict] = []
    cur: dict[str, Any] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("-"):
            if cur:
                items.append(cur)
            cur = {}
            rest = s[1:].strip()
            if ":" in rest:
                k, v = rest.split(":", 1)
                cur[k.strip()] = v.strip().strip('"').strip("'")
        elif ":" in s and cur is not None:
            k, v = s.split(":", 1)
            cur[k.strip()] = v.strip().strip('"').strip("'")
    if cur:
        items.append(cur)
    out = []
    for it in items:
        try:
            out.append({
                "selector": str(it.get("selector") or ""),
                "assert": str(it.get("assert") or "visible"),
                "timeout": int(str(it.get("timeout") or 8000)),
            })
        except ValueError:
            continue
    return [x for x in out if x["selector"]]


def candidates(platform: str) -> list[dict]:
    key = str(platform or "").strip().lower()
    path = ADAPTER_DIR / f"{key}.yaml"
    if path.is_file():
        try:
            parsed = _parse_simple_yaml(path.read_text(encoding="utf-8"))
            if parsed:
                return parsed
        except OSError:
            pass
    return list(BUILTIN.get(key, []))


def refresh_from_remote(timeout: float = 15.0) -> dict:
    """远端签名拉取适配包，HMAC-SHA256 校验。返回 {updated, error}。"""
    base = str(os.environ.get("FLOWOPS_ADAPTER_URL") or "").strip().rstrip("/")
    secret = str(os.environ.get("FLOWOPS_ADAPTER_KEY") or "")
    if not base:
        return {"updated": 0, "error": "未配置 FLOWOPS_ADAPTER_URL"}
    import urllib.request
    updated = 0
    errors: list[str] = []
    for plat in ("douyin", "xiaohongshu", "channels"):
        url = f"{base}/adapters/{plat}.yaml"
        try:
            req = urllib.request.Request(url, headers={"X-FlowOps-Platform": plat})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "ignore")
                sig = resp.headers.get("X-FlowOps-Signature", "")
                if secret:
                    expect = hmac_lib.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
                    if not hmac_lib.compare_digest(expect, sig):
                        errors.append(f"{plat}: 签名校验失败")
                        continue
            ADAPTER_DIR.mkdir(parents=True, exist_ok=True)
            (ADAPTER_DIR / f"{plat}.yaml").write_text(body, encoding="utf-8")
            updated += 1
        except Exception as exc:
            errors.append(f"{plat}: {str(exc)[:120]}")
    return {"updated": updated, "error": "；".join(errors)}


def drift_pack(platform: str, url: str, tried: list[str], dom: str = "",
               screenshot: bytes | None = None) -> dict:
    """SelectorDrift 诊断包：URL/选择器/DOM/截图，打包为 JSON，一键导出。"""
    pack = {
        "platform": platform,
        "url": url,
        "tried": list(tried or []),
        "dom_len": len(dom or ""),
        "dom_head": (dom or "")[:20000],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "has_screenshot": bool(screenshot),
    }
    if screenshot:
        pack["screenshot_bytes"] = len(screenshot)
    return pack


def save_drift_pack(pack: dict, screenshot: bytes | None = None) -> str:
    """一键导出：落盘到 logs/selector-drift-<platform>-<ts>.json，截图另存 png。"""
    outdir = BASE / "logs" / "selector-drift"
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    ts = time.strftime("%Y%m%d-%H%M%S")
    plat = str(pack.get("platform") or "unknown")
    path = outdir / f"{plat}-{ts}.json"
    blob = dict(pack)
    if screenshot and not blob.get("has_screenshot"):
        blob["has_screenshot"] = True
        blob["screenshot_bytes"] = len(screenshot)
    shot_path = ""
    if screenshot:
        shot_path = str(outdir / f"{plat}-{ts}.png")
        try:
            with open(shot_path, "wb") as fh:
                fh.write(screenshot)
            blob["screenshot_path"] = shot_path
        except OSError:
            pass
    try:
        path.write_text(json.dumps(blob, ensure_ascii=False, indent=2)[:200000],
                        encoding="utf-8")
    except OSError:
        pass
    return str(path)


async def wait_first_visible_async(page, platform: str, default_timeout: int = 12000) -> dict:
    """按 adapters 有序候选依次 wait_for_selector，命中即返；全失败抛 SelectorDrift。"""
    from utils.errors import SelectorDriftError
    tried: list[str] = []
    for cand in candidates(platform):
        sel = str(cand.get("selector") or "")
        if not sel:
            continue
        tried.append(sel)
        try:
            await page.wait_for_selector(sel, timeout=int(cand.get("timeout") or default_timeout))
            return {"selector": sel, "assert": cand.get("assert")}
        except Exception:
            continue
    # 全失败：打包诊断信息，MTTR 从天降到 2 小时内
    url, dom = "", ""
    shot = None
    try:
        url = str(page.url or "")
    except Exception:
        pass
    try:
        dom = str(await page.content())[:60000]
    except Exception:
        pass
    try:
        shot = await page.screenshot(full_page=False)
    except Exception:
        shot = None
    pack = drift_pack(platform, url, tried, dom, shot)
    path = save_drift_pack(pack, shot)
    raise SelectorDriftError(f"选择器全部失效({platform})，诊断包已导出：{path}")


def wait_first_visible_sync(page, platform: str, default_timeout: int = 12000) -> dict:
    from utils.errors import SelectorDriftError
    tried: list[str] = []
    for cand in candidates(platform):
        sel = str(cand.get("selector") or "")
        if not sel:
            continue
        tried.append(sel)
        try:
            page.wait_for_selector(sel, timeout=int(cand.get("timeout") or default_timeout))
            return {"selector": sel, "assert": cand.get("assert")}
        except Exception:
            continue
    url, dom = "", ""
    shot = None
    try:
        url = str(page.url or "")
    except Exception:
        pass
    try:
        dom = str(page.content())[:60000]
    except Exception:
        pass
    try:
        shot = page.screenshot(full_page=False)
    except Exception:
        shot = None
    pack = drift_pack(platform, url, tried, dom, shot)
    path = save_drift_pack(pack, shot if isinstance(shot, (bytes, bytearray)) else None)
    raise SelectorDriftError(f"选择器全部失效({platform})，诊断包已导出：{path}")
