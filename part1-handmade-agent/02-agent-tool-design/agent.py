"""
02-agent-tool-design

循环和上一篇相同：带 tools 问模型 → 执行 → 写回 messages。
这篇只把工具换成「搜酒店 / 订房间」，用来看 schema 怎么约束模型。

要点：
- description 写清何时用、参数从哪来；数组顺序不决定调用顺序
- enum / required / additionalProperties 收紧取值
- 模型给的 arguments 是字符串，dispatch 里先解析再校验再执行
- hotel_id 必须来自本次搜索结果，防止模型自造一个去下单
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from openai import OpenAI


def _load_dotenv() -> None:
    here = Path(__file__).resolve()
    root = next((p for p in here.parents if (p / ".git").exists()), here.parent)
    path = root / ".env"  # 仓库根那一份，不写死往上几层
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


_load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
)
MODEL = os.getenv("MODEL", "qwen/qwen-2.5-72b-instruct")

SYSTEM_PROMPT = (
    "你是酒店预订助手。房价、空房、hotel_id 只能来自工具，不要编造。"
    "用户只是询问有没有房、多少钱时，只搜索、不下单。"
    "明确要求预订时，用搜索返回的 hotel_id 调用 book_hotel。"
    "给出终答，带上酒店名、房型、入住日期和总价。"
)
GOAL = "帮我订东京一间豪华套房，10 月 1 日住两晚。"

# 本地假库存。真实项目里这是下游服务；这里用来演示校验，不打外部订房接口。
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
    {
        "id": "HT-003",
        "name": "浅草文化酒店",
        "city": "东京",
        "room_types": ["standard"],
        "price_per_night": {"standard": 110},
    },
]

ROOM_TYPES = ("standard", "deluxe", "suite")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 本次 run() 里搜索过的 hotel_id。book_hotel 只接受名单内的 id。
seen_hotel_ids: set[str] = set()


def search_hotels(city: str, check_in: str) -> dict:
    """只读。返回裁过的字段，避免把整份库存塞进上下文。"""
    if not DATE_RE.match(check_in):
        return {"error": "check_in 必须是 YYYY-MM-DD，例如 2026-10-01"}
    try:
        datetime.strptime(check_in, "%Y-%m-%d")
    except ValueError:
        return {"error": f"check_in 不是合法日期: {check_in}"}

    hits = []
    for h in INVENTORY:
        if city in h["city"] or h["city"] in city:
            seen_hotel_ids.add(h["id"])
            hits.append(
                {
                    "id": h["id"],
                    "name": h["name"],
                    "city": h["city"],
                    "room_types": h["room_types"],
                    "price_per_night": h["price_per_night"],
                }
            )
    if not hits:
        return {"error": f"没有找到城市 {city} 的可订酒店，请换一个城市名"}
    return {"check_in": check_in, "hotels": hits}


def book_hotel(hotel_id: str, room_type: str, check_in: str, nights: int) -> dict:
    """写操作。hotel_id 必须是本次 search_hotels 返回过的。"""
    if hotel_id not in seen_hotel_ids:
        return {
            "error": (
                f"hotel_id {hotel_id} 不在本次搜索结果里。"
                f"请先 search_hotels，可用 id：{sorted(seen_hotel_ids) or '（还没有，先搜）'}"
            )
        }
    hotel = next((h for h in INVENTORY if h["id"] == hotel_id), None)
    if hotel is None:
        return {"error": f"库存里没有 {hotel_id}"}
    if room_type not in hotel["room_types"]:
        return {
            "error": f"{hotel['name']} 没有 {room_type}，可选：{hotel['room_types']}"
        }
    if not DATE_RE.match(check_in):
        return {"error": "check_in 必须是 YYYY-MM-DD，例如 2026-10-01"}
    if not isinstance(nights, int) or nights < 1 or nights > 14:
        return {"error": "nights 必须是 1 到 14 的整数"}

    unit = hotel["price_per_night"][room_type]
    return {
        "status": "ok",
        "hotel_id": hotel_id,
        "hotel_name": hotel["name"],
        "room_type": room_type,
        "check_in": check_in,
        "nights": nights,
        "total": unit * nights,
        "currency": "USD",
    }


REGISTRY = {
    "search_hotels": search_hotels,
    "book_hotel": book_hotel,
}

# 顺序故意把 book 放前面。模型应靠 description 先搜后订，而不是按数组下标。
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "book_hotel",
            "description": (
                "用 search_hotels 返回的 hotel_id 预订房间。"
                "没有 hotel_id 时不要编一个，先搜。"
                "用户只是询问有没有房或多少钱时不要调用。"
                "room_type 只能是 standard / deluxe / suite。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hotel_id": {
                        "type": "string",
                        "description": "search_hotels 返回的 id，形如 HT-001，不是酒店名",
                    },
                    "room_type": {
                        "type": "string",
                        "enum": list(ROOM_TYPES),
                        "description": "房型，只能从枚举里选",
                    },
                    "check_in": {
                        "type": "string",
                        "description": "入住日期，格式 YYYY-MM-DD，例如 2026-10-01",
                    },
                    "nights": {
                        "type": "integer",
                        "description": "入住晚数，1 到 14 的整数",
                    },
                },
                "required": ["hotel_id", "room_type", "check_in", "nights"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_hotels",
            "description": (
                "按城市和入住日期搜索可订酒店，返回 id、房型和每晚价格。"
                "用户只给了城市名、还没有 hotel_id 时先调它。"
                "不要用它下单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名，如 东京、Tokyo",
                    },
                    "check_in": {
                        "type": "string",
                        "description": "入住日期，格式 YYYY-MM-DD，例如 2026-10-01",
                    },
                },
                "required": ["city", "check_in"],
                "additionalProperties": False,
            },
        },
    },
]


def _validate(name: str, args: dict) -> Optional[str]:
    """schema 是软约束。这里做硬校验，失败信息必须写出期望值。"""
    schema = next(
        s["function"] for s in TOOL_SCHEMAS if s["function"]["name"] == name
    )
    props = schema["parameters"]["properties"]
    required = schema["parameters"]["required"]

    missing = [k for k in required if k not in args]
    if missing:
        return f"缺少参数 {missing}，必填：{required}"

    extra = [k for k in args if k not in props]
    if extra:
        return f"多余参数 {extra}，合法键：{list(props)}"

    if "room_type" in args and args["room_type"] not in ROOM_TYPES:
        return (
            f"room_type 必须是 {' / '.join(ROOM_TYPES)}，"
            f"收到了「{args['room_type']}」"
        )
    if "check_in" in args and not DATE_RE.match(str(args["check_in"])):
        return f"check_in 必须是 YYYY-MM-DD，收到了「{args['check_in']}」"
    if "nights" in args:
        nights = args["nights"]
        if not isinstance(nights, int) or isinstance(nights, bool):
            return f"nights 必须是整数，收到了 {type(nights).__name__}"
        if nights < 1 or nights > 14:
            return "nights 必须是 1 到 14 的整数"
    return None


def dispatch(name: str, raw_args: str) -> dict:
    fn = REGISTRY.get(name)
    if fn is None:
        return {"error": f"没有名为 {name} 的工具，可用：{list(REGISTRY)}"}
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return {"error": f"参数不是合法 JSON: {raw_args[:200]}"}
    if not isinstance(args, dict):
        return {"error": "参数必须是 JSON 对象"}
    err = _validate(name, args)
    if err:
        return {"error": err}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": f"参数不匹配: {e}"}


def run(goal: str, max_turns: int = 8, token_budget: int = 20_000) -> str:
    seen_hotel_ids.clear()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]
    used = 0

    for turn in range(1, max_turns + 1):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOL_SCHEMAS, tool_choice="auto"
        )
        usage = resp.usage
        if usage is not None:
            used += usage.total_tokens
        msg = resp.choices[0].message
        print(f"[turn {turn}] tokens={used} tool_calls={bool(msg.tool_calls)}")

        if not msg.tool_calls:
            return msg.content or ""
        if used > token_budget:
            return f"超出 token 预算（{used}），已中止。"

        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )
        for tc in msg.tool_calls:
            result = dispatch(tc.function.name, tc.function.arguments)
            print(f"  → {tc.function.name}({tc.function.arguments}) = {result}")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    return f"达到最大轮数（{max_turns}）仍未收敛，已中止。"


if __name__ == "__main__":
    print(run(GOAL))
