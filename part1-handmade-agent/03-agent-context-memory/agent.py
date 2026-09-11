"""
03-agent-context-memory

循环仍是：带 tools 问模型 → 执行 → 写回 messages。
这篇加两件事：
- compact_messages：旧的 tool 消息不能删（否则 400），只把 content 换成摘要
- Memory：目标、搜到的 id、失败过的参数，放在循环外，每轮写进 system

搜索结果故意带一段很长的 blurb，用来对比压缩前后的体积。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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

# 最近几轮（一轮 = assistant.tool_calls + 对应的 tool）保持原文，更早的才压缩。
KEEP_ROUNDS = 1

SYSTEM_PROMPT = (
    "你是酒店预订助手。房价、空房、hotel_id 只能来自工具或记忆，不要编造。"
    "用户只是询问有没有房、多少钱时，只搜索、不下单。"
    "明确要求预订时，用记忆或搜索里的 hotel_id 调用 book_hotel。"
    "给出终答，带上酒店名、房型、入住日期和总价。"
)
GOAL = "帮我订东京一间 suite，10 月 1 日住两晚。"

# blurb 下一步用不到，压缩时会被裁掉。
INVENTORY = [
    {
        "id": "HT-001",
        "name": "东京站格兰酒店",
        "city": "东京",
        "room_types": ["standard", "deluxe"],
        "price_per_night": {"standard": 180, "deluxe": 260},
        "blurb": "车站步行 3 分钟。" * 40,
    },
    {
        "id": "HT-002",
        "name": "新宿京王广场",
        "city": "东京",
        "room_types": ["standard", "deluxe", "suite"],
        "price_per_night": {"standard": 150, "deluxe": 220, "suite": 400},
        "blurb": "邻近地铁口，适合商务出行。" * 40,
    },
    {
        "id": "HT-003",
        "name": "浅草文化酒店",
        "city": "东京",
        "room_types": ["standard"],
        "price_per_night": {"standard": 110},
        "blurb": "雷门附近，适合观光。" * 40,
    },
]

ROOM_TYPES = ("standard", "deluxe", "suite")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Memory:
    """循环外的记忆。只记结论，不记工具原文。"""

    def __init__(self, goal: str) -> None:
        self.goal = goal
        self.hotel_ids: list[str] = []
        self.hotels: list[dict[str, Any]] = []  # id / name / room_types / price
        self.rejected: list[str] = []
        self.booked: Optional[dict[str, Any]] = None

    def remember_search(self, city: str, hotels: list[dict[str, Any]]) -> None:
        # 换城市就覆盖，避免旧 id 和新结果叠在一起。
        self.hotel_ids = [h["id"] for h in hotels]
        self.hotels = hotels

    def remember_reject(self, reason: str) -> None:
        if reason not in self.rejected:
            self.rejected.append(reason)

    def remember_booking(self, receipt: dict[str, Any]) -> None:
        self.booked = {
            "hotel_id": receipt.get("hotel_id"),
            "hotel_name": receipt.get("hotel_name"),
            "total": receipt.get("total"),
        }

    def as_prompt(self) -> str:
        payload = {
            "goal": self.goal,
            "hotel_ids": self.hotel_ids,
            "hotels": self.hotels,
            "rejected": self.rejected,
            "booked": self.booked,
        }
        return "记忆：\n" + json.dumps(payload, ensure_ascii=False)


memory: Optional[Memory] = None


def search_hotels(city: str, check_in: str) -> dict:
    if not DATE_RE.match(check_in):
        return {"error": "check_in 必须是 YYYY-MM-DD，例如 2026-10-01"}
    try:
        datetime.strptime(check_in, "%Y-%m-%d")
    except ValueError:
        return {"error": f"check_in 不是合法日期: {check_in}"}

    hits = []
    for h in INVENTORY:
        if city in h["city"] or h["city"] in city:
            hits.append(
                {
                    "id": h["id"],
                    "name": h["name"],
                    "city": h["city"],
                    "room_types": h["room_types"],
                    "price_per_night": h["price_per_night"],
                    # 决策用不到，但很多真实 API 会塞这类字段。
                    "blurb": h["blurb"],
                }
            )
    if not hits:
        return {"error": f"没有找到城市 {city} 的可订酒店，请换一个城市名"}
    if memory is not None:
        memory.remember_search(
            city,
            [
                {
                    "id": x["id"],
                    "name": x["name"],
                    "room_types": x["room_types"],
                    "price_per_night": x["price_per_night"],
                }
                for x in hits
            ],
        )
    return {"check_in": check_in, "hotels": hits}


def book_hotel(hotel_id: str, room_type: str, check_in: str, nights: int) -> dict:
    allowed = set(memory.hotel_ids) if memory is not None else set()
    if hotel_id not in allowed:
        if memory is not None:
            memory.remember_reject(f"hotel_id {hotel_id} 不在搜索结果")
        return {
            "error": (
                f"hotel_id {hotel_id} 不在本次搜索结果里。"
                f"请先 search_hotels，可用 id：{sorted(allowed) or '（还没有，先搜）'}"
            )
        }
    hotel = next((h for h in INVENTORY if h["id"] == hotel_id), None)
    if hotel is None:
        return {"error": f"库存里没有 {hotel_id}"}
    if room_type not in hotel["room_types"]:
        if memory is not None:
            memory.remember_reject(f"{hotel['name']} 无 {room_type}")
        return {
            "error": f"{hotel['name']} 没有 {room_type}，可选：{hotel['room_types']}"
        }
    if not DATE_RE.match(check_in):
        return {"error": "check_in 必须是 YYYY-MM-DD，例如 2026-10-01"}
    if not isinstance(nights, int) or nights < 1 or nights > 14:
        return {"error": "nights 必须是 1 到 14 的整数"}

    unit = hotel["price_per_night"][room_type]
    receipt = {
        "status": "ok",
        "hotel_id": hotel_id,
        "hotel_name": hotel["name"],
        "room_type": room_type,
        "check_in": check_in,
        "nights": nights,
        "total": unit * nights,
        "currency": "USD",
    }
    if memory is not None:
        memory.remember_booking(receipt)
    return receipt


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
        if memory is not None:
            memory.remember_reject(err)
        return {"error": err}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": f"参数不匹配: {e}"}


def _estimate_chars(messages: list[dict]) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)


def _summarize_tool_content(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return json.dumps(
            {"_compacted": True, "summary": raw[:80]}, ensure_ascii=False
        )
    if isinstance(data, dict) and data.get("error"):
        return json.dumps(
            {"_compacted": True, "error": data["error"]}, ensure_ascii=False
        )
    if isinstance(data, dict) and "hotels" in data:
        ids = [h.get("id") for h in data.get("hotels") or [] if isinstance(h, dict)]
        return json.dumps(
            {
                "_compacted": True,
                "summary": f"{len(ids)} 家酒店",
                "hotel_ids": ids,
            },
            ensure_ascii=False,
        )
    if isinstance(data, dict) and data.get("status") == "ok":
        return json.dumps(
            {
                "_compacted": True,
                "summary": f"已订 {data.get('hotel_name')}，总价 {data.get('total')}",
            },
            ensure_ascii=False,
        )
    return json.dumps({"_compacted": True, "summary": "旧 tool 返回已压缩"}, ensure_ascii=False)


def compact_messages(messages: list[dict], keep_rounds: int = KEEP_ROUNDS) -> list[dict]:
    """按「轮」压缩，不要从头部按条数切。

    一轮 = 一条带 tool_calls 的 assistant + 后面若干 role=tool。
    最近 keep_rounds 轮保留原文；更早的 tool 只改 content，不删消息。
    """
    starts = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    if len(starts) <= keep_rounds:
        return messages

    protect = set()
    for start in starts[-keep_rounds:]:
        protect.add(start)
        j = start + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            protect.add(j)
            j += 1

    compacted = []
    for i, m in enumerate(messages):
        if m.get("role") == "tool" and i not in protect:
            new_m = dict(m)
            new_m["content"] = _summarize_tool_content(m.get("content") or "")
            compacted.append(new_m)
        else:
            compacted.append(m)
    return compacted


def with_memory(system_prompt: str, mem: Memory) -> dict:
    return {"role": "system", "content": system_prompt + "\n\n" + mem.as_prompt()}


def run(goal: str, max_turns: int = 8, token_budget: int = 20_000) -> str:
    global memory
    memory = Memory(goal)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]
    used = 0

    for turn in range(1, max_turns + 1):
        messages[0] = with_memory(SYSTEM_PROMPT, memory)
        outgoing = compact_messages(messages)
        print(
            f"[turn {turn}] raw_chars={_estimate_chars(messages)} "
            f"sent_chars={_estimate_chars(outgoing)}"
        )

        resp = client.chat.completions.create(
            model=MODEL, messages=outgoing, tools=TOOL_SCHEMAS, tool_choice="auto"
        )
        usage = resp.usage
        if usage is not None:
            used += usage.total_tokens
        msg = resp.choices[0].message
        print(f"  tokens={used} tool_calls={bool(msg.tool_calls)}")

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
