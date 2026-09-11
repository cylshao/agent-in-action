"""
09-agent-full-loop / loop

循环这一层：把前面各层按固定顺序串起来，一次 run 从进循环到出口。

  1. 进循环带上 run_id；新目标新建记忆，确认之后读回上一轮留下的那份
  2. 写记忆进 system → 压缩出副本 → 问模型；问模型超时则终止
  3. 没有 tool_calls：给出终答。终答在问确认就停住，等一条新的 user
  4. 有 tool_calls：先过护栏。打转终止；未确认的写操作回灌 error，不执行
  5. 放过的只读工具轮内并发、带超时，按请求顺序回灌
  6. 写操作单独执行，带幂等键；超时先查订单再决定要不要订
  7. 打一行轨迹日志，按 run_id 写审计
  8. 累计 token 按这次 run 自己加，超了终止

顺序不能换的三处：压缩在问模型之前，护栏在执行之前，审计在回灌之后。
"""

from __future__ import annotations

from typing import Optional

from audit import log_turn, read_audit, write_audit
from config import (
    GOAL,
    MAX_TURNS,
    MCP_PROMPT,
    MODEL,
    MODEL_TIMEOUT,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    RUN_ID,
    SYSTEM_PROMPT,
    TOKEN_BUDGET,
)
from executor import execute, tool_messages
from guards import biz_calls, is_spin, screen
from mcp_tools import McpTools
from memory import Memory, compact_messages
from toolset import BUSINESS_TOOLS, schemas


def ask_model(client, messages: list[dict], tools: list[dict]):
    return client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        timeout=MODEL_TIMEOUT,
    )


def run(
    goal: str = GOAL,
    run_id: str = RUN_ID,
    resume: bool = False,
    mcp: Optional[McpTools] = None,
    max_turns: int = MAX_TURNS,
    token_budget: int = TOKEN_BUDGET,
) -> str:
    from openai import OpenAI  # 只当 HTTP 客户端用

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)

    system_prompt = SYSTEM_PROMPT + (MCP_PROMPT if mcp else "")
    tools = schemas(mcp)  # 外部工具的 schema 在进循环之前问一次，之后不再变

    if resume:
        # 续跑：同一个 run_id、同一份 messages 和记忆，接一条 user。
        messages, mem = read_audit(run_id)
        mem.confirmed = True
        messages.append({"role": "user", "content": "确认"})
    else:
        mem = Memory(goal, run_id)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": goal},
        ]

    for turn in range(1, max_turns + 1):
        messages[0] = {
            "role": "system",
            "content": system_prompt + "\n\n" + mem.as_prompt(),
        }
        outgoing = compact_messages(messages, keep_rounds=1)

        try:
            resp = ask_model(client, outgoing, tools)
        except Exception as e:  # 问模型超时：这一轮没有 tool_calls，没有配对可做
            write_audit(run_id, messages, mem)
            return f"问模型失败或超时，已终止：{type(e).__name__} {e}"

        usage = resp.usage
        if usage is not None:
            mem.used_tokens += usage.total_tokens
        msg = resp.choices[0].message
        print(
            f"[run {run_id} turn {turn}] tokens={mem.used_tokens} "
            f"confirmed={mem.confirmed} plan={[s['status'] for s in mem.plan]}"
        )

        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            write_audit(run_id, messages, mem)
            if not mem.confirmed:
                # 终答在问确认。循环停住，不是终止：messages 留着，等那条 user。
                print(f"停住等确认：{run_id}，{len(messages)} 条 messages")
            return msg.content or ""

        if mem.used_tokens > token_budget:
            write_audit(run_id, messages, mem)
            return f"累计 token 超了（{mem.used_tokens}），已终止。"

        pending = [
            {"id": tc.id, "name": tc.function.name, "args": tc.function.arguments}
            for tc in msg.tool_calls
        ]

        # 护栏：打转在执行之前就终止，不再回灌同一份 error 让它接着转。
        calls = biz_calls(messages) + [
            (p["name"], p["args"]) for p in pending if p["name"] in BUSINESS_TOOLS
        ]
        if is_spin(calls):
            write_audit(run_id, messages, mem)
            return f"打转：{calls[-1][0]} 同一组参数连调，已终止。"

        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": p["id"],
                        "type": "function",
                        "function": {"name": p["name"], "arguments": p["args"]},
                    }
                    for p in pending
                ],
            }
        )
        # 先筛后执行：护栏拦下的不进执行，但仍要占一条回灌，按请求顺序合回去。
        allowed, blocked = screen(pending, mem.confirmed)
        tool_msgs = tool_messages(pending, {**execute(mem, allowed, mcp), **blocked})
        messages.extend(tool_msgs)
        log_turn(run_id, turn, pending, tool_msgs)
        write_audit(run_id, messages, mem)

    write_audit(run_id, messages, mem)
    return f"达到最大轮数（{max_turns}）仍未收敛，已终止。"
