"""
07-agent-reliability-cost

循环不改。护栏在 dispatch 之前：打转就终止，未确认的写操作不执行。
累计 token 按每轮发出去的 messages 体积加总。

要点（与文章同一套叫法）：
- 误差累积 = 每多一个业务工具，做成的概率按连乘掉
- 打转 = 连续两次业务工具，名字和参数都相同
- 护栏 = dispatch 之前拦 tool_calls
- 确认 = 一条新的 user 消息，允许执行这次写操作
- 累计 token = 第 1 轮到当前轮，每次请求体积之和
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


BUSINESS_TOOLS = ("get_weather", "search_hotels", "book_hotel")
WRITE_TOOLS = ("book_hotel",)


@dataclass
class Call:
    name: str
    args: dict[str, Any]
    result: dict[str, Any]


def extract_calls(messages: list[dict[str, Any]]) -> list[Call]:
    by_id = {m["tool_call_id"]: m for m in messages if m.get("role") == "tool" and m.get("tool_call_id")}
    calls: list[Call] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"_raw": raw}
            if not isinstance(args, dict):
                args = {"_raw": raw}
            try:
                result = json.loads((by_id.get(tc.get("id"), {}) or {}).get("content") or "{}")
            except json.JSONDecodeError:
                result = {}
            if not isinstance(result, dict):
                result = {}
            calls.append(Call(name=fn.get("name", ""), args=args, result=result))
    return calls


def success_rate(p: float, steps: int) -> float:
    return p**steps


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return max(1, len(json.dumps(messages, ensure_ascii=False)) // 4)


def cumulative_sent(rounds: list[list[dict[str, Any]]]) -> list[int]:
    total = 0
    out: list[int] = []
    for sent in rounds:
        total += estimate_tokens(sent)
        out.append(total)
    return out


def is_spin(calls: list[Call]) -> bool:
    biz = [c for c in calls if c.name in BUSINESS_TOOLS]
    if len(biz) < 2:
        return False
    a, b = biz[-2], biz[-1]
    return a.name == b.name and a.args == b.args


def unconfirmed_write(calls: list[Call], confirmed: bool) -> bool:
    return any(c.name in WRITE_TOOLS for c in calls) and not confirmed


def guard(calls: list[Call], confirmed: bool) -> tuple[str, str]:
    """过 / 不过。不过时不执行当前 tool_calls。打转则终止。"""
    if is_spin(calls):
        return "不过", "打转：同一业务工具、同一组参数，终止"
    if unconfirmed_write(calls, confirmed):
        return "不过", "写操作未确认，不执行 book_hotel"
    return "过", "护栏未拦"


def _tool(tid: str, name: str, args: dict, result: dict) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tid,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": tid,
            "content": json.dumps(result, ensure_ascii=False),
        },
    ]


def _base() -> list[dict]:
    return [
        {"role": "system", "content": "规则 + 记忆"},
        {"role": "user", "content": "东京 10 月 1 日住两晚。下雨订 suite，否则订 standard。"},
    ]


HOTELS = {
    "check_in": "2026-10-01",
    "hotels": [{"id": "HT-002", "name": "新宿京王广场"}],
}
WEATHER = {"city": "东京", "date": "2026-10-01", "condition": "rain", "high": 18}
BOOK = {
    "status": "ok",
    "hotel_id": "HT-002",
    "hotel_name": "新宿京王广场",
    "room_type": "suite",
    "total": 800,
}
BOOK_ARGS = {
    "hotel_id": "HT-002",
    "room_type": "suite",
    "check_in": "2026-10-01",
    "nights": 2,
}


def trace_ok() -> list[dict]:
    msgs = _base()
    msgs += _tool("c1", "set_plan", {"steps": ["查天气", "搜酒店", "订房"]}, {"plan": []})
    msgs += _tool("c2", "get_weather", {"city": "东京", "date": "2026-10-01"}, WEATHER)
    msgs += _tool("c3", "search_hotels", {"city": "东京", "check_in": "2026-10-01"}, HOTELS)
    msgs += _tool("c4", "book_hotel", BOOK_ARGS, BOOK)
    msgs.append({"role": "assistant", "content": "已订新宿京王广场 suite。"})
    return msgs


def trace_spin() -> list[dict]:
    msgs = _base()
    msgs += _tool("c1", "set_plan", {"steps": ["查天气", "搜酒店", "订房"]}, {"plan": []})
    args = {"city": "东京", "date": "2026-10-01"}
    msgs += _tool("c2", "get_weather", args, WEATHER)
    msgs += _tool("c3", "get_weather", args, WEATHER)
    msgs += _tool("c4", "get_weather", args, WEATHER)
    msgs.append({"role": "assistant", "content": "东京 10 月 1 日下雨。"})
    return msgs


def trace_unconfirmed() -> list[dict]:
    msgs = _base()
    msgs += _tool("c1", "set_plan", {"steps": ["查天气", "搜酒店", "订房"]}, {"plan": []})
    msgs += _tool("c2", "get_weather", {"city": "东京", "date": "2026-10-01"}, WEATHER)
    msgs += _tool("c3", "search_hotels", {"city": "东京", "check_in": "2026-10-01"}, HOTELS)
    msgs += _tool("c4", "book_hotel", BOOK_ARGS, BOOK)
    msgs.append({"role": "assistant", "content": "已订新宿京王广场 suite。"})
    return msgs


def grow_rounds(n: int) -> list[list[dict]]:
    """每轮多一对 assistant / tool，用来看累计体积。"""
    rounds: list[list[dict]] = []
    msgs = _base()
    rounds.append([m.copy() for m in msgs])
    for i in range(n):
        msgs += _tool(f"t{i}", "search_hotels", {"city": "东京", "check_in": "2026-10-01"}, HOTELS)
        rounds.append([m.copy() for m in msgs])
    return rounds


def main() -> None:
    print("误差累积（单步 0.95）")
    for steps in (1, 2, 3, 5, 10, 20):
        print(f"  业务工具 {steps:>2} 步 → {success_rate(0.95, steps):.1%}")
    print()

    print("累计 token（每轮重发全部 messages，体积/4）")
    sent = cumulative_sent(grow_rounds(5))
    prev = 0
    for i, used in enumerate(sent, start=1):
        this = used - prev
        print(f"  第 {i} 轮发出 {this}，累计 {used}")
        prev = used
    print(f"  第 6 轮累计 / 第 1 轮 ≈ {sent[-1] / sent[0]:.1f} 倍（线性 6 倍解释不了）")
    print()

    cases = [
        ("ok", trace_ok, True),
        ("spin", trace_spin, True),
        ("unconfirmed", trace_unconfirmed, False),
    ]
    print(f"{'对照轨迹':<14} {'护栏':<6} 说明")
    for name, build, confirmed in cases:
        calls = extract_calls(build())
        status, reason = guard(calls, confirmed)
        print(f"{name:<14} {status:<6} {reason}")


if __name__ == "__main__":
    main()
