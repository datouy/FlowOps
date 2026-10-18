# -*- coding: utf-8 -*-
"""AI 文案生成：支持多行业、多平台，通过 OpenAI Responses 兼容接口调用。"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

PLATFORM_GUIDE = {
    "小红书": "小红书种草体：标题 20-30 字，带情绪和具体数字；正文分段、少量 emoji、结尾引导评论；话题 5-8 个。",
    "抖音": "抖音口播脚本：前 3 秒抓眼球，口语化、短句，适合朗读；标题 15-25 字；话题 3-5 个。",
    "视频号": "视频号文案：稳重、真诚、适合 30-60 岁人群；讲清楚价值和细节；标题 15-25 字；话题 2-4 个。",
    "快手": "快手文案：直白、实在、强调性价比，适合口播；标题 15-25 字；话题 3-5 个。",
    "微博": "微博文案：标题 30 字以内，正文简洁有话题感；话题 2-4 个。",
    "百家号": "百家号文案：资讯/推荐风格，逻辑清晰，标题 20-30 字；话题 2-4 个。",
}

INDUSTRY_PROFILES = {
    "旅行社": {
        "role": "资深旅行社新媒体运营",
        "focus": "行程透明、纯玩体验、住宿交通、适合人群和真实服务细节",
        "taboo": "不要编造行程、价格、购物安排或服务承诺",
    },
    "餐饮美食": {
        "role": "餐饮门店与美食内容运营",
        "focus": "口味、食材、招牌菜、环境、到店体验和适合场景",
        "taboo": "不要编造优惠、销量、排名或获奖信息",
    },
    "美妆个护": {
        "role": "美妆护肤内容运营",
        "focus": "成分、适用肤质、使用步骤、使用感受和搭配建议",
        "taboo": "不要宣称医疗功效、绝对效果或替代药品",
    },
    "教育培训": {
        "role": "教育机构内容运营",
        "focus": "课程内容、师资、学习方法、适合人群和试听体验",
        "taboo": "不要承诺提分、升学结果或保过",
    },
    "健身运动": {
        "role": "健身教练与运动内容运营",
        "focus": "训练动作、计划安排、适合人群、注意事项和坚持体验",
        "taboo": "不要承诺减重、治疗效果或无条件适用",
    },
    "本地生活": {
        "role": "本地生活商家内容运营",
        "focus": "服务项目、位置、环境、价格、营业时间和预约方式",
        "taboo": "不要编造优惠、地址、营业时间或服务范围",
    },
    "电商零售": {
        "role": "电商内容运营",
        "focus": "产品卖点、使用场景、规格、适用人群和售后信息",
        "taboo": "不要编造参数、销量、排名、最低价或绝对效果",
    },
    "房产家居": {
        "role": "房产家居内容运营",
        "focus": "空间、设计、材质、预算、适用家庭和生活场景",
        "taboo": "不要承诺升值、收益或绝对品质",
    },
    "其他": {
        "role": "专业新媒体运营",
        "focus": "真实卖点、目标人群、使用场景和行动引导",
        "taboo": "不要编造未提供的信息",
    },
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


def _call_with_retry(prompt: str, max_tokens: int = 1200) -> str:
    last_error = None
    for attempt in range(3):
        try:
            return _call_llm(prompt, max_tokens)
        except RuntimeError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1)
    raise last_error


def _industry(data: dict) -> str:
    industry = (data.get("industry") or "旅行社").strip()
    if industry == "其他":
        custom = (data.get("custom_industry") or "").strip()
        return custom or "其他"
    return industry


def _fields(data: dict, industry: str) -> dict:
    name = (data.get("business_name") or data.get("tour_name") or data.get("product_name") or "").strip()
    topic = (data.get("topic") or "").strip()
    if not topic and not name:
        raise ValueError("请输入主题或名称")
    days = str(data.get("days") or "").strip() if industry == "旅行社" else ""
    return {
        "topic": topic,
        "name": name or topic,
        "days": days,
        "price": str(data.get("price") or "").strip(),
        "highlights": (data.get("highlights") or "").strip(),
        "audience": (data.get("audience") or "").strip(),
        "tone": (data.get("tone") or "真实、亲切").strip(),
    }


def _clean_json(text: str) -> str:
    fence = chr(96) * 3
    cleaned = text.strip().replace(fence + "json", "").replace(fence, "").strip()
    match = re.search(r"\{.*\}", cleaned, re.S)
    return match.group(0) if match else cleaned


def _parse_tags(value) -> list:
    tags = value or []
    if isinstance(tags, str):
        tags = [t.strip().lstrip("#") for t in re.split(r"[,，\s]+", tags) if t.strip()]
    return [str(t).lstrip("#") for t in tags][:10]


TITLE_LIMITS = {"小红书": 30, "抖音": 25, "视频号": 25, "快手": 25, "微博": 30, "百家号": 30}


def _template_content(platform, industry, fields, profile):
    name = fields.get("name") or fields.get("topic") or industry
    highlights = fields.get("highlights") or profile.get("focus") or "真实体验"
    audience = fields.get("audience") or ""
    price = fields.get("price") or ""
    limit = TITLE_LIMITS.get(platform, 30)
    title = name + "｜" + highlights.split("、")[0].split(",")[0]
    title = title[:limit]
    lines = ["【" + name + "】"]
    if audience:
        lines.append("适合：" + audience)
    lines.append("亮点：" + highlights)
    if price:
        lines.append("参考价格：" + price)
    lines.append("想了解详情，可以在评论区留言或私信关键词【" + industry + "】。")
    body = "\n\n".join(lines)
    tags = []
    for raw in [industry, name, audience, "真实体验", "实用分享"]:
        raw = (raw or "").strip()
        if raw and raw not in tags:
            tags.append(raw[:10])
    return {
        "title": title,
        "body": body,
        "tags": tags[:6],
        "tip": "AI 接口暂时不可用，已使用本地模板生成，请人工润色后发布。",
        "platform": platform,
        "industry": industry,
        "model": None,
        "fallback": True,
    }




def generate_content_with_ai(data: dict) -> dict:
    platform = (data.get("platform") or "小红书").strip()
    industry = _industry(data)
    profile = INDUSTRY_PROFILES.get(industry, INDUSTRY_PROFILES["其他"])
    fields = _fields(data, industry)
    guide = PLATFORM_GUIDE.get(platform, PLATFORM_GUIDE["小红书"])
    context = [
        "行业：" + industry,
        "主题：" + fields["topic"],
        "名称/产品：" + fields["name"],
        "目标人群：" + fields["audience"],
        "价格：" + fields["price"],
        "卖点：" + fields["highlights"],
        "语气：" + fields["tone"],
    ]
    if fields["days"]:
        context.append("天数：" + fields["days"] + "天")
    prompt = (
        "你是一名" + profile["role"] + "。请生成一条" + platform + "发布用的文案。\n"
        + "\n".join(context) + "\n"
        + "行业重点：" + profile["focus"] + "\n"
        + "平台要求：" + guide + "\n\n"
        + "禁忌：" + profile["taboo"] + "；不要使用\u201c最低价\u201d\u201c保证\u201d等绝对化表述。\n"
        + "只输出 JSON，不要代码块，格式："
        + '{"title":"标题","body":"正文，可含换行","tags":["话题不带#号"],"tip":"发布建议一句"}'
    )
    try:
        text = _call_with_retry(prompt)
    except RuntimeError:
        return _template_content(platform, industry, fields, profile)
    cleaned = _clean_json(text)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        parsed = {"title": text.splitlines()[0][:60], "body": text, "tags": [], "tip": ""}
    title = str(parsed.get("title") or "").strip()[:TITLE_LIMITS.get(platform, 30)]
    tags = _parse_tags(parsed.get("tags"))
    if not tags:
        tags = [x for x in [industry, (fields.get("name") or "")[:10], "实用分享"] if x]
    return {
        "title": title,
        "body": str(parsed.get("body") or "").strip(),
        "tags": tags,
        "tip": str(parsed.get("tip") or "").strip(),
        "platform": platform,
        "industry": industry,
        "model": _load_env().get("WB_MODEL"),
    }



def generate_content(data: dict) -> dict:
    return generate_content_with_ai(data)


def _fallback_pack(data: dict, platforms: list, industry: str) -> dict:
    generated = {}
    errors = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(generate_content, dict(data, platform=platform)): platform
            for platform in platforms
        }
        for future in as_completed(futures):
            platform = futures[future]
            try:
                generated[platform] = future.result()
            except Exception as exc:
                errors.append(platform + ": " + str(exc)[:120])
    result = []
    for platform in platforms:
        one = generated.get(platform) or {}
        result.append({
            "platform": platform,
            "title": one.get("title", ""),
            "body": one.get("body", ""),
            "tags": one.get("tags", []),
        })
    return {
        "items": result,
        "model": _load_env().get("WB_MODEL"),
        "industry": industry,
        "fallback": True,
        "errors": errors,
    }


def generate_pack(data: dict) -> dict:
    """一次生成 6 个平台的内容；失败时降级为逐平台生成。"""
    platforms = ["小红书", "抖音", "视频号", "快手", "微博", "百家号"]
    industry = _industry(data)
    profile = INDUSTRY_PROFILES.get(industry, INDUSTRY_PROFILES["其他"])
    fields = _fields(data, industry)
    guides = "\n".join("- " + p + "：" + PLATFORM_GUIDE[p] for p in platforms)
    context = [
        "行业：" + industry,
        "主题：" + fields["topic"],
        "名称/产品：" + fields["name"],
        "目标人群：" + fields["audience"],
        "价格：" + fields["price"],
        "卖点：" + fields["highlights"],
    ]
    if fields["days"]:
        context.append("天数：" + fields["days"] + "天")
    prompt = (
        "你是" + profile["role"] + "。请围绕以下信息，一次生成 6 个平台的内容。\n"
        + "\n".join(context) + "\n\n"
        + "行业重点：" + profile["focus"] + "\n"
        + "平台要求：\n" + guides + "\n\n"
        + "禁忌：" + profile["taboo"] + "；不要使用绝对化用语。\n"
        + "只输出 JSON，不要代码块。格式："
        + '{"小红书":{"title":"","body":"","tags":[]},"抖音":{"title":"","body":"","tags":[]},"视频号":{"title":"","body":"","tags":[]},"快手":{"title":"","body":"","tags":[]},"微博":{"title":"","body":"","tags":[]},"百家号":{"title":"","body":"","tags":[]}}'
    )
    try:
        text = _call_with_retry(prompt, 3000)
    except RuntimeError:
        return _fallback_pack(data, platforms, industry)
    cleaned = _clean_json(text)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return _fallback_pack(data, platforms, industry)
    result = []
    for platform in platforms:
        item = parsed.get(platform) or {}
        result.append({
            "platform": platform,
            "title": str(item.get("title") or "").strip(),
            "body": str(item.get("body") or "").strip(),
            "tags": _parse_tags(item.get("tags")),
        })
    return {"items": result, "model": _load_env().get("WB_MODEL"), "industry": industry}


def industries() -> list:
    return list(INDUSTRY_PROFILES.keys())

