"""
06-agent-eval

循环不改。检查不调模型，从 messages 抽出轨迹，对轨迹跑检查。

要点（与文章同一套叫法）：
- 轨迹 = 按时间排开的 tool_calls + 回灌
- 检查 = 对轨迹跑的一条确定性规则
- 对照轨迹 = 写好的 messages，用来验检查本身
- 真跑 = 进循环调模型走完一次 run，再用同一套检查判过不过
- 答案级只看终答；轨迹级看跳步、参数来源、房型

用法：
    python eval.py                     跑五条对照轨迹
    python eval.py run.json [...]      把真跑留下的 messages 丢进同一套检查
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


BUSINESS_TOOLS = ("get_weather", "search_hotels", "book_hotel")
PLAN_TOOLS = ("set_plan", "mark_step")


@dataclass
class Call:
    name: str
    args: dict[str, Any]
    result: dict[str, Any]


@dataclass
class CheckResult:
    name: str
    ok: bool
    reason: str


def extract_calls(messages: list[dict[str, Any]]) -> list[Call]:
    """一条 assistant.tool_calls 必须配对相同 tool_call_id 的 tool。"""
    by_id: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tid = msg.get("tool_call_id")
        if tid:
            by_id[tid] = msg

    calls: list[Call] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            tid = tc.get("id")
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"_raw": raw}
            if not isinstance(args, dict):
                args = {"_raw": raw}
            tool_msg = by_id.get(tid, {})
            try:
                result = json.loads(tool_msg.get("content") or "{}")
            except json.JSONDecodeError:
                result = {"_raw": tool_msg.get("content")}
            if not isinstance(result, dict):
                result = {"_raw": result}
            calls.append(Call(name=fn.get("name", ""), args=args, result=result))
    return calls


def check_plan_first(calls: list[Call]) -> CheckResult:
    for c in calls:
        if c.name in BUSINESS_TOOLS:
            return CheckResult("计划优先", False, f"先调了业务工具 {c.name}，没有 set_plan")
        if c.name == "set_plan":
            return CheckResult("计划优先", True, "第一个业务工具之前有 set_plan")
    return CheckResult("计划优先", False, "轨迹里没有 set_plan")


def check_no_skip(calls: list[Call]) -> CheckResult:
    seen: set[str] = set()
    for c in calls:
        if c.name not in BUSINESS_TOOLS:
            continue
        if c.name == "search_hotels" and "get_weather" not in seen:
            return CheckResult("跳步", False, "search_hotels 之前没有 get_weather")
        if c.name == "book_hotel" and "get_weather" not in seen:
            return CheckResult("跳步", False, "book_hotel 之前没有 get_weather")
        if c.name == "book_hotel" and "search_hotels" not in seen:
            return CheckResult("跳步", False, "book_hotel 之前没有 search_hotels")
        seen.add(c.name)
    if "book_hotel" not in seen:
        return CheckResult("跳步", False, "没有 book_hotel")
    return CheckResult("跳步", True, "天气 → 搜索 → 订房，没有跳")


def check_hotel_id_source(calls: list[Call]) -> CheckResult:
    allowed: list[str] = []
    for c in calls:
        if c.name == "search_hotels" and "error" not in c.result:
            for h in c.result.get("hotels") or []:
                hid = h.get("id")
                if hid:
                    allowed.append(hid)
        if c.name == "book_hotel":
            hid = c.args.get("hotel_id")
            if hid not in allowed:
                return CheckResult(
                    "参数来源",
                    False,
                    f"book_hotel 用了 {hid}，不在此前 search_hotels 返回里",
                )
    if not any(c.name == "book_hotel" for c in calls):
        return CheckResult("参数来源", False, "没有 book_hotel")
    return CheckResult("参数来源", True, "hotel_id 来自本次 search_hotels")


def check_room_from_weather(calls: list[Call]) -> CheckResult:
    weather = None
    for c in calls:
        if c.name == "get_weather" and "error" not in c.result:
            weather = c.result.get("condition")
    book = next((c for c in reversed(calls) if c.name == "book_hotel"), None)
    if weather is None:
        return CheckResult("房型", False, "没有可用的 get_weather 返回")
    if book is None:
        return CheckResult("房型", False, "没有 book_hotel")
    room = book.args.get("room_type")
    expect = "suite" if weather == "rain" else "standard"
    if room != expect:
        return CheckResult("房型", False, f"天气 {weather} 应订 {expect}，订了 {room}")
    return CheckResult("房型", True, f"天气 {weather}，房型 {room}")


CHECKS: list[Callable[[list[Call]], CheckResult]] = [
    check_plan_first,
    check_no_skip,
    check_hotel_id_source,
    check_room_from_weather,
]


def evaluate(messages: list[dict[str, Any]]) -> list[CheckResult]:
    calls = extract_calls(messages)
    return [fn(calls) for fn in CHECKS]


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


def _reply(text: str) -> dict:
    return {"role": "assistant", "content": text}


HOTELS = {
    "check_in": "2026-10-01",
    "hotels": [
        {"id": "HT-001", "name": "东京站格兰酒店"},
        {"id": "HT-002", "name": "新宿京王广场"},
    ],
}
WEATHER_RAIN = {"city": "东京", "date": "2026-10-01", "condition": "rain", "high": 18}
BOOK_OK = {
    "status": "ok",
    "hotel_id": "HT-002",
    "hotel_name": "新宿京王广场",
    "room_type": "suite",
    "total": 800,
}
REPLY_OK = "东京 10 月 1 日下雨，已订新宿京王广场 suite，两晚 800 美元。"


def trace_ok() -> list[dict]:
    msgs: list[dict] = [
        {"role": "system", "content": "规则 + 记忆"},
        {"role": "user", "content": "东京 10 月 1 日住两晚。下雨订 suite，否则订 standard。"},
    ]
    msgs += _tool("c1", "set_plan", {"steps": ["查天气", "搜酒店", "订房"]}, {"plan": []})
    msgs += _tool("c2", "get_weather", {"city": "东京", "date": "2026-10-01"}, WEATHER_RAIN)
    msgs += _tool("c3", "mark_step", {"index": 1, "status": "done"}, {"plan": []})
    msgs += _tool("c4", "search_hotels", {"city": "东京", "check_in": "2026-10-01"}, HOTELS)
    msgs += _tool("c5", "mark_step", {"index": 2, "status": "done"}, {"plan": []})
    msgs += _tool(
        "c6",
        "book_hotel",
        {"hotel_id": "HT-002", "room_type": "suite", "check_in": "2026-10-01", "nights": 2},
        BOOK_OK,
    )
    msgs += _tool("c7", "mark_step", {"index": 3, "status": "done"}, {"plan": []})
    msgs.append(_reply(REPLY_OK))
    return msgs


def trace_skip() -> list[dict]:
    """终答和 ok 一样，但没查天气、没搜就订了。"""
    msgs: list[dict] = [
        {"role": "system", "content": "规则 + 记忆"},
        {"role": "user", "content": "东京 10 月 1 日住两晚。下雨订 suite，否则订 standard。"},
    ]
    msgs += _tool("c1", "set_plan", {"steps": ["查天气", "搜酒店", "订房"]}, {"plan": []})
    msgs += _tool(
        "c2",
        "book_hotel",
        {"hotel_id": "HT-002", "room_type": "suite", "check_in": "2026-10-01", "nights": 2},
        BOOK_OK,
    )
    msgs.append(_reply(REPLY_OK))
    return msgs


def trace_fabricated_id() -> list[dict]:
    msgs: list[dict] = [
        {"role": "system", "content": "规则 + 记忆"},
        {"role": "user", "content": "东京 10 月 1 日住两晚。下雨订 suite，否则订 standard。"},
    ]
    msgs += _tool("c1", "set_plan", {"steps": ["查天气", "搜酒店", "订房"]}, {"plan": []})
    msgs += _tool("c2", "get_weather", {"city": "东京", "date": "2026-10-01"}, WEATHER_RAIN)
    msgs += _tool("c3", "search_hotels", {"city": "东京", "check_in": "2026-10-01"}, HOTELS)
    msgs += _tool(
        "c4",
        "book_hotel",
        {"hotel_id": "HT-999", "room_type": "suite", "check_in": "2026-10-01", "nights": 2},
        {"error": "hotel_id HT-999 不在本次搜索结果里"},
    )
    msgs.append(_reply("已按 suite 订好 HT-999。"))
    return msgs


def trace_wrong_room() -> list[dict]:
    msgs: list[dict] = [
        {"role": "system", "content": "规则 + 记忆"},
        {"role": "user", "content": "东京 10 月 1 日住两晚。下雨订 suite，否则订 standard。"},
    ]
    msgs += _tool("c1", "set_plan", {"steps": ["查天气", "搜酒店", "订房"]}, {"plan": []})
    msgs += _tool("c2", "get_weather", {"city": "东京", "date": "2026-10-01"}, WEATHER_RAIN)
    msgs += _tool("c3", "search_hotels", {"city": "东京", "check_in": "2026-10-01"}, HOTELS)
    booked = {**BOOK_OK, "room_type": "standard", "total": 300}
    msgs += _tool(
        "c4",
        "book_hotel",
        {"hotel_id": "HT-002", "room_type": "standard", "check_in": "2026-10-01", "nights": 2},
        booked,
    )
    msgs.append(_reply("东京 10 月 1 日已订新宿京王广场 standard，两晚 300 美元。"))
    return msgs


def trace_no_plan() -> list[dict]:
    msgs: list[dict] = [
        {"role": "system", "content": "规则 + 记忆"},
        {"role": "user", "content": "东京 10 月 1 日住两晚。下雨订 suite，否则订 standard。"},
    ]
    msgs += _tool("c1", "get_weather", {"city": "东京", "date": "2026-10-01"}, WEATHER_RAIN)
    msgs += _tool("c2", "search_hotels", {"city": "东京", "check_in": "2026-10-01"}, HOTELS)
    msgs += _tool(
        "c3",
        "book_hotel",
        {"hotel_id": "HT-002", "room_type": "suite", "check_in": "2026-10-01", "nights": 2},
        BOOK_OK,
    )
    msgs.append(_reply(REPLY_OK))
    return msgs


CASES = [
    ("ok", trace_ok),
    ("skip", trace_skip),
    ("fabricated_id", trace_fabricated_id),
    ("wrong_room", trace_wrong_room),
    ("no_plan", trace_no_plan),
]


def _final_reply(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            return msg.get("content") or ""
    return ""


def report(name: str, messages: list[dict]) -> None:
    reply = _final_reply(messages)
    answer_ok = "订" in reply and ("suite" in reply or "standard" in reply or "HT-" in reply)
    results = evaluate(messages)
    traj_ok = all(r.ok for r in results)
    print(f"{name:<16} {'过' if answer_ok else '不过':<6} 轨迹级 {'过' if traj_ok else '不过'}")
    for r in results:
        print(f"  [{'过' if r.ok else '不过'}] {r.name}: {r.reason}")
    print()


def main() -> None:
    paths = sys.argv[1:]
    if paths:
        # 真跑：把一次 run 出口时的 messages（或按 run_id 留下的审计）丢进同一套检查。
        # 留的必须是未压缩的那份，压缩副本里回灌原文没了，参数来源会对不上。
        print(f"{'真跑':<16} {'答案级':<6} 检查")
        for p in paths:
            messages = json.loads(Path(p).read_text(encoding="utf-8"))
            report(Path(p).stem, messages)
        return

    print(f"{'对照轨迹':<16} {'答案级':<6} 检查")
    for name, build in CASES:
        report(name, build())


if __name__ == "__main__":
    main()
