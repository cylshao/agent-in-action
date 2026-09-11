"""
09-agent-full-loop / guards

护栏这一层：拿到 tool_calls 之后、执行之前。

判断只看参数，不需要执行结果——放到执行之后，房已经订了。
screen 把这一轮分成两堆：放过的交给 executor，拦下的直接给出要回灌的 error。
拦下来的那条仍要占一条 role=tool，配对不能断。
"""

from __future__ import annotations

from toolset import BUSINESS_TOOLS, is_write


def biz_calls(messages: list[dict]) -> list[tuple[str, str]]:
    """(工具名, 参数原文)，只看业务工具。计划工具反复调不算打转。"""
    out = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            name = tc["function"]["name"]
            if name in BUSINESS_TOOLS:
                out.append((name, tc["function"]["arguments"]))
    return out


def is_spin(calls: list[tuple[str, str]]) -> bool:
    """连续两次业务工具，名字和参数都相同。换了参数是重试，不算。"""
    if len(calls) < 2:
        return False
    return calls[-1] == calls[-2]


def screen(pending: list[dict], confirmed: bool) -> tuple[list[dict], dict[str, dict]]:
    """返回 (放过的, {tool_call_id: 要回灌的 error})。"""
    allowed, blocked = [], {}
    for item in pending:
        if is_write(item["name"]) and not confirmed:
            blocked[item["id"]] = {"error": "写操作未确认，不执行 book_hotel"}
        else:
            allowed.append(item)
    return allowed, blocked
