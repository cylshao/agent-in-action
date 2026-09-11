"""
09-agent-full-loop / checks

检查这一层：不在循环里，事后跑审计留下的 messages。

轨迹级不是答案级：终答说「已订好」不算过，要看跳步、参数来源、房型对不对。
检查不调模型，也不认识循环里的任何一层：它只认审计文件的格式，加上一份工具分类。
路径由入口传进来——这不是循环的一层，是读产物的另一个程序。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from toolset import BUSINESS_TOOLS


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


def extract_calls(messages: list[dict]) -> list[Call]:
    """一条 assistant.tool_calls 必须配对相同 tool_call_id 的 tool。"""
    by_id = {m["tool_call_id"]: m for m in messages if m.get("role") == "tool"}

    def parse(raw: Any) -> dict:
        try:
            value = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"_raw": raw}
        return value if isinstance(value, dict) else {"_raw": value}

    calls = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls.append(
                Call(
                    name=fn.get("name", ""),
                    args=parse(fn.get("arguments")),
                    result=parse((by_id.get(tc.get("id")) or {}).get("content")),
                )
            )
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
        if c.name in ("search_hotels", "book_hotel") and "get_weather" not in seen:
            return CheckResult("跳步", False, f"{c.name} 之前没有 get_weather")
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
            allowed += [h.get("id") for h in c.result.get("hotels") or []]
        if c.name == "book_hotel":
            hid = c.args.get("hotel_id")
            if hid not in allowed:
                return CheckResult(
                    "参数来源", False, f"book_hotel 用了 {hid}，不在此前 search_hotels 返回里"
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


CHECKS = [check_plan_first, check_no_skip, check_hotel_id_source, check_room_from_weather]


def evaluate(messages: list[dict]) -> list[CheckResult]:
    calls = extract_calls(messages)
    return [fn(calls) for fn in CHECKS]


def check_audit(paths: list[str]) -> None:
    for p in paths:
        results = evaluate(json.loads(Path(p).read_text(encoding="utf-8")))
        print(f"{Path(p).stem}  轨迹级 {'过' if all(r.ok for r in results) else '不过'}")
        for r in results:
            print(f"  [{'过' if r.ok else '不过'}] {r.name}: {r.reason}")
