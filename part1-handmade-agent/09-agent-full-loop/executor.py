"""
09-agent-full-loop / executor

执行这一层：护栏放过之后，到回灌进 messages 之前。收到的都是能执行的。

- 只读工具轮内并发，每次带超时；超时也是一条回灌。
- 写操作单独执行，带幂等键；超时先查订单再决定要不要订。
- 每个结果当场 absorb 进记忆：同一轮里先搜后订，订房才看得见这次的 hotel_id。
- tool_messages 按请求顺序排，与完成先后无关，否则下一轮 400。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Optional

import toolset
from config import MCP_TIMEOUT, TOOL_TIMEOUT
from mcp_tools import McpTools
from memory import Memory


def timeout_for(name: str, mcp: Optional[McpTools]) -> float:
    """外部工具要连进程、跑网络，时限比本地函数松。"""
    return MCP_TIMEOUT if mcp is not None and mcp.handles(name) else TOOL_TIMEOUT


def call_with_timeout(fn, timeout: float, *a, **kw) -> dict:
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *a, **kw)
    try:
        return fut.result(timeout=timeout)
    except (TimeoutError, FuturesTimeout):
        return {"error": "超时"}
    finally:
        ex.shutdown(wait=False)


def execute(
    mem: Memory, allowed: list[dict], mcp: Optional[McpTools] = None
) -> dict[str, dict]:
    """返回 {tool_call_id: 结果}。谁先完成谁先进字典，排序是下一步的事。"""
    results: dict[str, dict] = {}
    readonly = [p for p in allowed if not toolset.is_write(p["name"])]
    writes = [p for p in allowed if toolset.is_write(p["name"])]

    if readonly:
        with ThreadPoolExecutor(max_workers=len(readonly)) as ex:
            futs = {
                ex.submit(
                    call_with_timeout,
                    toolset.call,
                    timeout_for(p["name"], mcp),
                    mem,
                    p["name"],
                    p["args"],
                    mcp,
                ): p
                for p in readonly
            }
            for fut, p in futs.items():
                results[p["id"]] = fut.result()
        # 并发跑完再一起进记忆：多个线程不同时改同一份状态。
        for p in readonly:
            mem.absorb(p["name"], results[p["id"]])

    # 写操作一次一个，读的是上面刚 absorb 进去的 hotel_ids。
    for p in writes:
        result = call_with_timeout(
            toolset.call, TOOL_TIMEOUT, mem, p["name"], p["args"], mcp
        )
        if result.get("error") == "超时":
            # 不要直接再订。先查这把幂等键。
            found = toolset.probe_write(mem, p["name"], p["args"])
            if found is not None:
                result = found
        mem.absorb(p["name"], result)
        results[p["id"]] = result

    return results


def tool_messages(pending: list[dict], results: dict[str, dict]) -> list[dict]:
    """按 pending 的请求顺序回灌，拦下的和执行的都在里面，一个不少。"""
    return [
        {
            "role": "tool",
            "tool_call_id": p["id"],
            "content": json.dumps(results[p["id"]], ensure_ascii=False),
        }
        for p in pending
    ]
