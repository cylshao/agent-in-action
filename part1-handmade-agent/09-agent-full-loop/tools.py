"""
09-agent-full-loop / tools

本地工具这一层：模型看见的是 schema，执行的是这里的函数。

这些函数都是纯的：只看参数，只返回结果，不碰记忆。要用记忆里的什么
（当前计划、这次搜索的 hotel_id、run_id），由 toolset 显式传进来；
结果怎么进记忆，是 memory.absorb 的事。校验不过就返回 error，由循环回灌。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ROOM_TYPES = ("standard", "deluxe", "suite")

INVENTORY = [
    {
        "id": "HT-001",
        "name": "东京站格兰酒店",
        "city": "东京",
        "room_types": ["standard", "deluxe"],
        "price_per_night": {"standard": 180, "deluxe": 260},
    },
    {
        "id": "HT-002",
        "name": "新宿京王广场",
        "city": "东京",
        "room_types": ["standard", "deluxe", "suite"],
        "price_per_night": {"standard": 150, "deluxe": 220, "suite": 400},
    },
]

# 本地假天气：东京 10-01 固定下雨，「下雨订 suite」这条线才复现得了。
# 换成真数据看 mcp_tools.py。
WEATHER = {
    ("东京", "2026-10-01"): {"condition": "rain", "high": 18},
    ("东京", "2026-10-02"): {"condition": "clear", "high": 22},
}

# 订房那一头的状态。键是幂等键，不是 agent 的记忆。
BOOKINGS: dict[str, dict[str, Any]] = {}


def idem_key(run_id: str, args: dict[str, Any]) -> str:
    return f"{run_id}:{args.get('hotel_id')}:{args.get('check_in')}"


def find_booking(key: str) -> Optional[dict[str, Any]]:
    return BOOKINGS.get(key)


# ---------- 计划工具 ----------


def set_plan(steps: list) -> dict:
    if not isinstance(steps, list) or not steps:
        return {"error": "steps 必须是非空字符串列表"}
    cleaned = []
    for s in steps:
        if not isinstance(s, str) or not s.strip():
            return {"error": "steps 每一项必须是非空字符串"}
        cleaned.append(s.strip())
    return {
        "plan": [
            {"index": i, "title": t, "status": "pending"}
            for i, t in enumerate(cleaned, start=1)
        ]
    }


def mark_step(index: int, status: str, note: str = "", *, plan: list) -> dict:
    if not isinstance(index, int) or isinstance(index, bool):
        return {"error": "index 必须是整数"}
    if status not in ("done", "failed"):
        return {"error": f"status 只能是 done / failed，收到了「{status}」"}
    updated = [dict(s) for s in plan]
    for step in updated:
        if step["index"] == index:
            step["status"] = status
            if note:
                step["note"] = note
            return {"plan": updated}
    return {"error": f"没有 index={index} 的步骤，当前：{[s['index'] for s in plan]}"}


# ---------- 业务工具 ----------


def get_weather(city: str, date: str) -> dict:
    if not DATE_RE.match(date):
        return {"error": "date 必须是 YYYY-MM-DD，例如 2026-10-01"}
    hit = WEATHER.get((city, date))
    if hit is None:
        return {"error": f"没有 {city} {date} 的天气，可查东京 2026-10-01 或 2026-10-02"}
    return {"city": city, "date": date, **hit}


def search_hotels(city: str, check_in: str) -> dict:
    if not DATE_RE.match(check_in):
        return {"error": "check_in 必须是 YYYY-MM-DD，例如 2026-10-01"}
    try:
        datetime.strptime(check_in, "%Y-%m-%d")
    except ValueError:
        return {"error": f"check_in 不是合法日期: {check_in}"}
    hits = [
        {
            "id": h["id"],
            "name": h["name"],
            "city": h["city"],
            "room_types": h["room_types"],
            "price_per_night": h["price_per_night"],
        }
        for h in INVENTORY
        if city in h["city"] or h["city"] in city
    ]
    if not hits:
        return {"error": f"没有找到城市 {city} 的可订酒店"}
    return {"check_in": check_in, "hotels": hits}


def book_hotel(
    hotel_id: str,
    room_type: str,
    check_in: str,
    nights: int,
    *,
    allowed_ids: list,
    run_id: str,
) -> dict:
    # hotel_id 白名单：id 必须来自这次 search_hotels 的回灌。确认只表示可以订。
    allowed = set(allowed_ids)
    if hotel_id not in allowed:
        return {
            "error": (
                f"hotel_id {hotel_id} 不在本次搜索结果里。"
                f"可用 id：{sorted(allowed) or '（还没有，先搜）'}"
            )
        }
    hotel = next((h for h in INVENTORY if h["id"] == hotel_id), None)
    if hotel is None:
        return {"error": f"库存里没有 {hotel_id}"}
    if room_type not in hotel["room_types"]:
        return {"error": f"{hotel['name']} 没有 {room_type}，可选：{hotel['room_types']}"}
    if not DATE_RE.match(check_in):
        return {"error": "check_in 必须是 YYYY-MM-DD"}
    if not isinstance(nights, int) or nights < 1 or nights > 14:
        return {"error": "nights 必须是 1 到 14 的整数"}

    key = idem_key(run_id, {"hotel_id": hotel_id, "check_in": check_in})
    existing = find_booking(key)
    if existing is not None:
        return {**existing, "idempotent": True}

    unit = hotel["price_per_night"][room_type]
    receipt = {
        "status": "ok",
        "booking_id": f"BK-{len(BOOKINGS) + 1:03d}",
        "hotel_id": hotel_id,
        "hotel_name": hotel["name"],
        "room_type": room_type,
        "check_in": check_in,
        "nights": nights,
        "total": unit * nights,
        "currency": "USD",
    }
    BOOKINGS[key] = receipt
    return receipt


# 模型只看见这一份 JSON：名字、说明、参数、必填、枚举。
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "set_plan",
            "description": (
                "写入或替换计划。没有计划、或要重规划时调用。"
                "steps 按执行顺序列出，每项一句话，不要写工具参数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "步骤标题列表，按执行顺序",
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_step",
            "description": (
                "把某一步的 status 改为 done 或 failed。"
                "对应的业务工具刚返回后调用；failed 时 note 写原因。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "步骤序号，从 1 起"},
                    "status": {"type": "string", "enum": ["done", "failed"]},
                    "note": {"type": "string", "description": "可选。failed 时写原因"},
                },
                "required": ["index", "status"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "按城市和日期查天气，返回 rain 或 clear。没有数据时不要编造。"
                "日期格式不对就换参数重试。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如 东京"},
                    "date": {"type": "string", "description": "日期，YYYY-MM-DD"},
                },
                "required": ["city", "date"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_hotels",
            "description": (
                "按城市和入住日期搜索可订酒店。还没有 hotel_id 时先调它。不要用它下单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如 东京"},
                    "check_in": {"type": "string", "description": "入住日期，YYYY-MM-DD"},
                },
                "required": ["city", "check_in"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_hotel",
            "description": (
                "用 search_hotels 返回的 hotel_id 预订。记忆里 confirmed 为 true 才调。"
                "room_type 只能是 standard / deluxe / suite。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hotel_id": {
                        "type": "string",
                        "description": "search_hotels 返回的 id，形如 HT-002",
                    },
                    "room_type": {"type": "string", "enum": list(ROOM_TYPES)},
                    "check_in": {"type": "string", "description": "入住日期，YYYY-MM-DD"},
                    "nights": {"type": "integer", "description": "入住晚数，1 到 14"},
                },
                "required": ["hotel_id", "room_type", "check_in", "nights"],
                "additionalProperties": False,
            },
        },
    },
]
