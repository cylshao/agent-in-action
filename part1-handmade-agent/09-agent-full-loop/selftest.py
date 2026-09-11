"""
09-agent-full-loop / selftest

不调模型，只跑各层里的纯函数：护栏、轮内并发的回灌顺序、幂等键、分流、压缩。
每一段都对应循环里的一步，用来先看清顺序，再去跑真的 run。
"""

from __future__ import annotations

import json
from typing import Optional

from config import GOAL
from executor import execute, tool_messages
from guards import is_spin, screen
from mcp_tools import McpTools
from memory import Memory, compact_messages
from tools import BOOKINGS
from toolset import schemas


def turn(
    mem: Memory, pending: list[dict], confirmed: bool, mcp: Optional[McpTools]
) -> list[dict]:
    """和 loop 里那三行一样：先筛，再执行，再按请求顺序合回去。"""
    allowed, blocked = screen(pending, confirmed)
    return tool_messages(pending, {**execute(mem, allowed, mcp), **blocked})


def selftest(mcp: Optional[McpTools] = None) -> None:
    mem = Memory(GOAL, "run-selftest")

    print("护栏 · 未确认的写操作")
    write = [{"id": "w1", "name": "book_hotel", "args": "{}"}]
    print("   未确认:", screen(write, confirmed=False))
    print("   确认后:", screen(write, confirmed=True))
    print("护栏 · 打转 vs 重试")
    same = [("get_weather", '{"city":"东京","date":"2026-10-01"}')] * 2
    other = [
        ("get_weather", '{"city":"东京","date":"2026-10-01"}'),
        ("get_weather", '{"city":"东京","date":"2026-10-02"}'),
    ]
    print("   同一组参数连调 →", is_spin(same), "（终止）")
    print("   换了日期 →", is_spin(other), "（重试）")

    print("轮内并发 · 回灌按请求顺序")
    pending = [
        {"id": "c1", "name": "search_hotels", "args": '{"city":"东京","check_in":"2026-10-01"}'},
        {"id": "c2", "name": "get_weather", "args": '{"city":"东京","date":"2026-10-01"}'},
        {
            "id": "c3",
            "name": "book_hotel",
            "args": '{"hotel_id":"HT-002","room_type":"suite","check_in":"2026-10-01","nights":2}',
        },
    ]
    msgs = turn(mem, pending, confirmed=False, mcp=None)
    print("   顺序:", " → ".join(m["tool_call_id"] for m in msgs))
    print("   c3:", msgs[2]["content"])
    print("   同一轮里搜到的 id 进了记忆:", mem.hotel_ids)

    print("确认之后 · 幂等键挡住双订")
    print("   第一次:", turn(mem, pending[2:], confirmed=True, mcp=None)[0]["content"])
    print("   再订一次:", turn(mem, pending[2:], confirmed=True, mcp=None)[0]["content"])
    print("   订单数:", len(BOOKINGS))

    print("dispatch · 按工具名分流")
    print("   模型看见的工具:", [t["function"]["name"] for t in schemas(mcp)])
    print("   get_weather 走:", "MCP call_tool" if mcp else "本地 REGISTRY")
    if mcp:
        print(
            "   外部回灌裁字段:",
            mcp.trim(
                "get_weather",
                {"date": "2026-10-01", "condition": "rain", "high": 18, "weathercode": 61},
            ),
        )

    print("压缩 · 旧的 tool 只改 content")
    messages = [
        {"role": "system", "content": "规则 + 记忆"},
        {"role": "user", "content": GOAL},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "search_hotels", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": json.dumps(
                {"hotels": [{"id": "HT-002", "blurb": "邻近地铁口" * 40}]}, ensure_ascii=False
            ),
        },
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c2",
            "content": json.dumps({"condition": "rain", "high": 18}, ensure_ascii=False),
        },
    ]
    out = compact_messages(messages, keep_rounds=1)
    print("   压缩前:", len(json.dumps(messages, ensure_ascii=False)), "字符")
    print("   压缩后:", len(json.dumps(out, ensure_ascii=False)), "字符")
    print("   c1 →", out[3]["content"])
    print("   c2 →", out[5]["content"], "（最近一轮留原文）")
    print("   配对仍在:", [m.get("tool_call_id") for m in out if m.get("role") == "tool"])
