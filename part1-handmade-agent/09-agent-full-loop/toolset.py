"""
09-agent-full-loop / toolset

工具目录与路由这一层，回答两个问题：

- 模型看见哪些工具：schemas()
- 一个工具名归谁执行：call() 里按名字分流，本地函数或外部 call_tool

本地实现在 tools.py，外部实现在 mcp_tools.py，两边互不认识，都挂在这一层下面。
工具的分类也放这儿：业务工具、写操作是工具集的事实，不是环境配置。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import tools
from mcp_tools import McpTools
from memory import Memory

# 护栏判打转、检查看轨迹，都只看业务工具，不看 set_plan / mark_step。
BUSINESS_TOOLS = ("get_weather", "search_hotels", "book_hotel", "search_location")
WRITE_TOOLS = ("book_hotel",)

REGISTRY: dict[str, Callable[..., dict]] = {
    "set_plan": tools.set_plan,
    "mark_step": tools.mark_step,
    "get_weather": tools.get_weather,
    "search_hotels": tools.search_hotels,
    "book_hotel": tools.book_hotel,
}

# 工具是纯函数：要用记忆里的什么，在这里显式取出来，当关键字参数传进去。
# 模型填的是 schema 里那几个参数，看不见也填不了这些。
STATE: dict[str, Callable[[Memory], dict[str, Any]]] = {
    "mark_step": lambda mem: {"plan": mem.plan},
    "book_hotel": lambda mem: {"allowed_ids": mem.hotel_ids, "run_id": mem.run_id},
}


def schemas(mcp: Optional[McpTools] = None) -> list[dict]:
    """模型看见的那一份。开了外部工具，本地同名的让位。"""
    if mcp is None:
        return tools.TOOL_SCHEMAS
    local = [t for t in tools.TOOL_SCHEMAS if not mcp.handles(t["function"]["name"])]
    return local + mcp.schemas()


def is_write(name: str) -> bool:
    return name in WRITE_TOOLS


def call(mem: Memory, name: str, raw_args: str, mcp: Optional[McpTools] = None) -> dict:
    """dispatch：按工具名分流。返回的东西还没进记忆，也没进 messages。"""
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return {"error": f"参数不是合法 JSON: {raw_args[:200]}"}
    if not isinstance(args, dict):
        return {"error": "参数必须是 JSON 对象"}

    if mcp is not None and mcp.handles(name):
        return mcp.call(name, args)

    fn = REGISTRY.get(name)
    if fn is None:
        available = list(REGISTRY) + (sorted(McpTools.ALLOW) if mcp else [])
        return {"error": f"没有名为 {name} 的工具，可用：{available}"}

    state = STATE.get(name)
    try:
        return fn(**args, **(state(mem) if state else {}))
    except TypeError as e:
        return {"error": f"参数不匹配: {e}"}


def probe_write(mem: Memory, name: str, raw_args: str) -> Optional[dict]:
    """写操作超时之后问一句：这把幂等键是不是已经有订单了。查到就别再订。"""
    if name != "book_hotel":
        return None
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        args = {}
    found = tools.find_booking(tools.idem_key(mem.run_id, args))
    return {**found, "idempotent": True} if found else None
