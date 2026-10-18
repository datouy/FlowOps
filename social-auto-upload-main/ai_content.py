# -*- coding: utf-8 -*-
"""AI 文案生成：调用 WorkBuddy Gateway（OpenAI Responses 协议）。"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent.resolve()
ENV_FILE = BASE / ".env"

DEFAULTS = {
    "WB_BASE_URL": "http://106.55.225.14/v1",
    "WB_MODEL": "deepseek-v4.1-flash",
    "WB_REALM": "cn",
}


def _load_env() -> dict:
    env = dict(DEFAULTS)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def config_status() -> dict:
    env = _load_env()
    return {
        "configured": bool(env.get("WB_API_KEY")),
        "model": env.get("WB_MODEL"),
        "base_url": env.get("WB_BASE_URL"),
    }


def _call_llm(prompt: str, max_tokens: int = 1200) -> str:
    env = _load_env()
    key = env.get("WB_API_KEY")
    if not key:
        raise RuntimeError("未配置 AI API Key，请在 .env 中设置 WB_API_KEY")
    payload = json.dumps({
        "model": env.get("WB_MODEL", DEFAULTS["WB_MODEL"]),
        "input": prompt,
        "max_output_tokens": max_tokens,
    }, ensure_ascii=False).encode("utf-8")
    realm = env.get("WB_REALM", "cn")
    url = env.get("WB_BASE_URL", DEFAULTS["WB_BASE_URL"]).rstrip("/") + "/responses?realm=" + realm
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "X-Realm": realm,
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"AI 接口返回 {exc.code}: {detail}")
    except Exception as exc:
        raise RuntimeError(f"AI 接口调用失败: {exc}")

    texts = []
    for item in data.get("output", []) or []:
        for block in item.get("content", []) or []:
            if block.get("type") == "output_text" and block.get("text"):
                texts.append(block["text"])
    result = "\n".join(texts) or data.get("output_text", "")
    if not result:
        raise RuntimeError("AI 返回内容为空")
    return result.strip()


PLATFORM_GUIDE = {
    "小红书": "小红书种草体：标题 20-30 字，带情绪和具体数字；正文分段、少量 emoji、结尾引导评论；话题 5-8 个。",
    "抖音": "抖音口播脚本：前 3 秒抓眼球，口语化、短句，适合朗读；标题 15-25 字；话题 3-5 个。",
    "视频号": "视频号文案：稳重、真诚、适合 30-60 岁人群；讲清楚行程与价值；标题 15-25 字；话题 2-4 个。",
    "快手": "快手文案：直白、实在、强调性价比，适合口播；标题 15-25 字；话题 3-5 个。",
    "微博": "微博文案：标题 30 字以内，正文简洁有话题感；话题 2-4 个。",
    "百家号": "百家号文案：资讯/推荐风格，逻辑清晰，标题 20-30 字；话题 2-4 个。",
}


def generate_content(data: dict) -> dict:
    platform = (data.get("platform") or "小红书").strip()
    topic = (data.get("topic") or "").strip()
    tone = (data.get("tone") or "真实、亲切").strip()
    name = (data.get("tour_name") or "").strip()
    days = str(data.get("days") or "").strip()
    price = str(data.get("price") or "").strip()
    highlights = (data.get("highlights") or "").strip()
    if not topic and not name:
        raise ValueError("请输入主题或线路名称")

    guide = PLATFORM_GUIDE.get(platform, PLATFORM_GUIDE["小红书"])
    prompt = (
        "你是一名资深旅行社新媒体运营。请为以下主题生成一条" + platform + "发布用的文案。\n"
        + "主题：" + topic + "\n"
        + "线路：" + (name or topic) + "\n"
        + "天数：" + days + "天\n"
        + "报价：" + price + "\n"
        + "亮点：" + highlights + "\n"
        + "语气：" + tone + "\n"
        + "平台要求：" + guide + "\n\n"
        + "禁忌：不要编造未提供的价格、行程、承诺；不要使用“最低价”“保证”等绝对化表述。\n"
        + "只输出 JSON，不要代码块，格式："
        + '{"title":"标题","body":"正文，可含换行","tags":["话题不带#号"],"tip":"发布建议一句"}'
    )
    try:
        text = _call_llm(prompt)
    except RuntimeError:
        text = _call_llm(prompt)
    cleaned = re.sub(r"^`{3}(?:json)?|`{3}$", "", text.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", cleaned, re.S)
    if match:
        cleaned = match.group(0)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        parsed = {"title": text.splitlines()[0][:60], "body": text, "tags": [], "tip": ""}
    tags = parsed.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip().lstrip("#") for t in re.split(r"[,，\s]+", tags) if t.strip()]
    return {
        "title": str(parsed.get("title") or "").strip(),
        "body": str(parsed.get("body") or "").strip(),
        "tags": [str(t).lstrip("#") for t in tags][:10],
        "tip": str(parsed.get("tip") or "").strip(),
        "platform": platform,
        "model": _load_env().get("WB_MODEL"),
    }

def generate_pack(data: dict) -> dict:
    """Generate platform-specific content for all supported platforms in one model call."""
    platforms = ["小红书", "抖音", "视频号", "快手", "微博", "百家号"]
    topic = (data.get("topic") or "").strip()
    name = (data.get("tour_name") or "").strip()
    if not topic and not name:
        raise ValueError("请输入主题或线路名称")
    guides = "\n".join("- " + p + "：" + PLATFORM_GUIDE[p] for p in platforms)
    prompt = (
        "你是资深旅行社新媒体运营。请围绕以下主题，一次生成 6 个平台的内容。\n"
        + "主题：" + topic + "\n"
        + "线路：" + (name or topic) + "\n"
        + "天数：" + str(data.get("days") or "") + "天\n"
        + "报价：" + str(data.get("price") or "") + "\n"
        + "亮点：" + str(data.get("highlights") or "") + "\n\n"
        + "平台要求：\n" + guides + "\n\n"
        + "禁止编造未提供的价格、行程、承诺；不要使用绝对化用语。\n"
        + "只输出 JSON，不要代码块。格式："
        + '{"小红书":{"title":"","body":"","tags":[]},"抖音":{"title":"","body":"","tags":[]},"视频号":{"title":"","body":"","tags":[]},"快手":{"title":"","body":"","tags":[]},"微博":{"title":"","body":"","tags":[]},"百家号":{"title":"","body":"","tags":[]}}'
    )
    try:
        text = _call_llm(prompt, max_tokens=4000)
    except RuntimeError:
        text = _call_llm(prompt, max_tokens=4000)
    fence = chr(96) * 3
    cleaned = text.strip().replace(fence + "json", "").replace(fence, "").strip()
    match = re.search(r"\{.*\}", cleaned, re.S)
    if match:
        cleaned = match.group(0)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        # Model occasionally returns malformed JSON; fall back to one call per platform.
        result = []
        for platform in platforms:
            one = generate_content(dict(data, platform=platform))
            result.append({
                "platform": platform,
                "title": one.get("title", ""),
                "body": one.get("body", ""),
                "tags": one.get("tags", []),
            })
        return {"items": result, "model": _load_env().get("WB_MODEL"), "fallback": True}
    result = []
    for platform in platforms:
        item = parsed.get(platform) or {}
        tags = item.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip().lstrip("#") for t in re.split(r"[,，\s]+", tags) if t.strip()]
        result.append({
            "platform": platform,
            "title": str(item.get("title") or "").strip(),
            "body": str(item.get("body") or "").strip(),
            "tags": [str(t).lstrip("#") for t in tags][:10],
        })
    return {"items": result, "model": _load_env().get("WB_MODEL")}


