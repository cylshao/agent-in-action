"""
05-agent-mcp-tools

循环仍是：带 tools 问模型 → 执行 → 写回 messages。
计划、记忆、压缩、本地订房与上一篇相同。
search_location / get_weather 不再进 REGISTRY：启动时 list_tools 拿 schema，dispatch 里 call_tool。

要点（与文章同一套叫法）：
- MCP Server：对外暴露 tools 的进程（本目录 mcp_server.py）
- MCP Client：agent 里连上 Server 的那一端
- 本地工具：set_plan、mark_step、search_hotels、book_hotel
- 外部工具：白名单里的 search_location、get_weather
- list_tools 的结果不可信，只转白名单里的名字
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastmcp import Client
from openai import OpenAI

HERE = Path(__file__).resolve().parent


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
    "你是出行助手。没有计划时，先调用 set_plan，不要先调业务工具。"
    "每轮只推进一个 status=pending 的步骤。"
    "步骤做成了就 mark_step(done)；做不到就 mark_step(failed)，再 set_plan。"
    "参数格式错、房型不在枚举里：不改计划，换参数重试。"
    "天气、房价、hotel_id 只能来自工具或记忆，不要编造。"
    "没有坐标时先 search_location，再 get_weather。经纬度不要编造。"
    "全部步骤 done 之后给出终答，带上天气、酒店名、房型、总价。"
)
GOAL = "东京 9 月 15 日住两晚。先查当天天气，下雨订 suite，否则订 standard。"

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
MCP_ALLOW = frozenset({"search_location", "get_weather"})


class Memory:
    """循环外的记忆。计划写在这里，每轮写进 system。"""

    def __init__(self, goal: str) -> None:
        self.goal = goal
        self.plan: list[dict[str, Any]] = []
        self.location: Optional[dict[str, Any]] = None
        self.weather: Optional[dict[str, Any]] = None
        self.hotel_ids: list[str] = []
        self.hotels: list[dict[str, Any]] = []
        self.rejected: list[str] = []
        self.booked: Optional[dict[str, Any]] = None

    def set_plan(self, steps: list[str]) -> dict[str, Any]:
        self.plan = [
            {"index": i, "title": title, "status": "pending"}
            for i, title in enumerate(steps, start=1)
        ]
        return {"plan": self.plan}

    def mark_step(self, index: int, status: str, note: str = "") -> dict[str, Any]:
        if status not in ("done", "failed"):
            return {"error": f"status 只能是 done / failed，收到了「{status}」"}
        for step in self.plan:
            if step["index"] == index:
                step["status"] = status
                if note:
                    step["note"] = note
                return {"plan": self.plan}
        return {
            "error": f"没有 index={index} 的步骤，当前计划：{[s['index'] for s in self.plan]}"
        }

    def remember_search(self, city: str, hotels: list[dict[str, Any]]) -> None:
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

    def remember_weather(self, payload: dict[str, Any]) -> None:
        self.weather = payload

    def remember_location(self, payload: dict[str, Any]) -> None:
        self.location = payload

    def as_prompt(self) -> str:
        payload = {
            "goal": self.goal,
            "plan": self.plan,
            "location": self.location,
            "weather": self.weather,
            "hotel_ids": self.hotel_ids,
            "hotels": self.hotels,
            "rejected": self.rejected,
            "booked": self.booked,
        }
        return "记忆：\n" + json.dumps(payload, ensure_ascii=False)


memory: Optional[Memory] = None


def set_plan(steps: list) -> dict:
    if memory is None:
        return {"error": "记忆未初始化"}
    if not isinstance(steps, list) or not steps:
        return {"error": "steps 必须是非空字符串列表"}
    cleaned = []
    for s in steps:
        if not isinstance(s, str) or not s.strip():
            return {"error": "steps 每一项必须是非空字符串"}
        cleaned.append(s.strip())
    return memory.set_plan(cleaned)


def mark_step(index: int, status: str, note: str = "") -> dict:
    if memory is None:
        return {"error": "记忆未初始化"}
    if not isinstance(index, int) or isinstance(index, bool):
        return {"error": "index 必须是整数"}
    return memory.mark_step(index, status, note)


def to_openai_tool(tool: Any) -> dict:
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    if schema is None:
        schema = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema,
        },
    }


async def load_mcp_schemas(mcp_client: Client) -> list[dict]:
    listed = await mcp_client.list_tools()
    items = getattr(listed, "tools", listed)
    schemas = []
    seen = set()
    for tool in items:
        if tool.name not in MCP_ALLOW:
            continue
        schemas.append(to_openai_tool(tool))
        seen.add(tool.name)
    missing = MCP_ALLOW - seen
    if missing:
        raise RuntimeError(f"MCP Server 缺少白名单里的工具：{sorted(missing)}")
    return schemas


def _tool_result_payload(result: Any) -> dict:
    if getattr(result, "is_error", False):
        texts = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                texts.append(text)
        return {"error": texts[0] if texts else "MCP Server 返回了 error"}
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    texts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    if not texts:
        return {"error": "MCP Server 没有返回可解析的内容"}
    try:
        parsed = json.loads(texts[0])
    except json.JSONDecodeError:
        return {"error": texts[0][:200]}
    return parsed if isinstance(parsed, dict) else {"error": texts[0][:200]}


# 回灌不可信：外部工具返回什么字段由 Server 说了算，进 messages 之前只留下一步要用的。
MCP_RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    "search_location": ("name", "latitude", "longitude", "timezone"),
    "get_weather": ("date", "condition", "high"),
}


def trim_payload(name: str, payload: dict) -> dict:
    keep = MCP_RESULT_FIELDS.get(name)
    if keep is None or "error" in payload:
        return payload
    trimmed = {k: v for k, v in payload.items() if k in keep}
    return trimmed or payload


async def call_mcp(mcp_client: Client, name: str, args: dict) -> dict:
    try:
        result = await mcp_client.call_tool(name, args)
    except Exception as e:
        return {"error": f"MCP call_tool 失败：{e}", "fatal": True}
    payload = trim_payload(name, _tool_result_payload(result))
    if "error" not in payload and memory is not None:
        if name == "search_location":
            memory.remember_location(payload)
        elif name == "get_weather":
            memory.remember_weather(payload)
    return payload


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
                }
            )
    if not hits:
        return {"error": f"没有找到城市 {city} 的可订酒店，请换一个城市名"}
    if memory is not None:
        memory.remember_search(city, hits)
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
    "set_plan": set_plan,
    "mark_step": mark_step,
    "search_hotels": search_hotels,
    "book_hotel": book_hotel,
}

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
                    "status": {
                        "type": "string",
                        "enum": ["done", "failed"],
                        "description": "做成了用 done，做不到用 failed",
                    },
                    "note": {
                        "type": "string",
                        "description": "可选。failed 时写原因",
                    },
                },
                "required": ["index", "status"],
                "additionalProperties": False,
            },
        },
    },
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


async def dispatch(mcp_client: Client, name: str, raw_args: str) -> dict:
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return {"error": f"参数不是合法 JSON: {raw_args[:200]}"}
    if not isinstance(args, dict):
        return {"error": "参数必须是 JSON 对象"}

    fn = REGISTRY.get(name)
    if fn is not None:
        err = _validate(name, args)
        if err:
            if memory is not None:
                memory.remember_reject(err)
            return {"error": err}
        try:
            return fn(**args)
        except TypeError as e:
            return {"error": f"参数不匹配: {e}"}

    if name in MCP_ALLOW:
        return await call_mcp(mcp_client, name, args)

    allowed = list(REGISTRY) + sorted(MCP_ALLOW)
    return {"error": f"没有名为 {name} 的工具，可用：{allowed}"}


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


async def run(
    mcp_client: Client,
    extra_schemas: list[dict],
    goal: str,
    max_turns: int = 12,
    token_budget: int = 24_000,
) -> str:
    global memory
    memory = Memory(goal)
    tool_schemas = TOOL_SCHEMAS + extra_schemas
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
            model=MODEL, messages=outgoing, tools=tool_schemas, tool_choice="auto"
        )
        usage = resp.usage
        if usage is not None:
            used += usage.total_tokens
        msg = resp.choices[0].message
        print(f"  tokens={used} plan={memory.plan} tool_calls={bool(msg.tool_calls)}")

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
            result = await dispatch(mcp_client, tc.function.name, tc.function.arguments)
            print(f"  → {tc.function.name}({tc.function.arguments}) = {result}")
            if result.get("fatal"):
                return f"终止：{result['error']}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    return f"达到最大轮数（{max_turns}）仍未收敛，已中止。"


async def main() -> None:
    async with Client(HERE / "mcp_server.py") as mcp_client:
        extra_schemas = await load_mcp_schemas(mcp_client)
        print(await run(mcp_client, extra_schemas, GOAL))


if __name__ == "__main__":
    asyncio.run(main())
