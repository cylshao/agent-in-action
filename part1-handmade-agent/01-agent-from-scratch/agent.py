"""
01-agent-from-scratch

循环是：带 tools 问模型 → 执行 → 写回 messages。
这篇用天气工具把循环跑通。三个出口：无 tool_calls、轮数上限、累计 token。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
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
http = httpx.Client(timeout=10.0)

SYSTEM_PROMPT = (
    "你是一个天气助手。只能通过工具获取事实，不要凭记忆编造温度或坐标。"
    "拿到数据后给出终答，带上温度、体感和湿度。"
)
GOAL = "东京现在天气怎么样？"


def search_location(name: str) -> dict:
    r = http.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": name, "count": 1, "language": "zh", "format": "json"},
    )
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        return {"error": f"找不到地点 {name}，请检查拼写"}
    top = results[0]
    return {
        "name": top.get("name"),
        "country": top.get("country"),
        "latitude": top.get("latitude"),
        "longitude": top.get("longitude"),
    }


def get_current_weather(latitude: float, longitude: float) -> dict:
    r = http.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m",
        },
    )
    r.raise_for_status()
    cur = r.json().get("current") or {}
    return {
        "temperature": cur.get("temperature_2m"),
        "apparent_temperature": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind_speed": cur.get("wind_speed_10m"),
    }


REGISTRY = {
    "search_location": search_location,
    "get_current_weather": get_current_weather,
}

# 顺序故意把天气放前面。模型应靠 description 先搜坐标再查天气，而不是按数组下标。
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "按经纬度获取当前天气。没有坐标时先用 search_location 拿到。",
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number", "description": "纬度，-90 到 90"},
                    "longitude": {"type": "number", "description": "经度，-180 到 180"},
                },
                "required": ["latitude", "longitude"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_location",
            "description": "按地名搜索地理坐标。当你只有城市名、需要经纬度时先调它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "地点名称，如 'Tokyo'、'北京'",
                    }
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
]


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
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": f"参数不匹配: {e}"}
    except httpx.HTTPError as e:
        return {"error": f"上游请求失败: {e}"}


def run(goal: str, max_turns: int = 8, token_budget: int = 20_000) -> str:
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
