"""
09-agent-full-loop / audit

留痕这一层：回灌之后。

- 轨迹日志：每轮当场打一行，带 run_id，用来现场分是配对、超时还是打转。
- 审计：每轮把这一份 messages 和记忆按 run_id 写下去，不等出口。
  写的是未压缩的那份——续跑要读它，事后的检查也要读它。
"""

from __future__ import annotations

import json

from config import AUDIT_DIR
from memory import Memory


def log_turn(run_id: str, turn: int, pending: list[dict], tool_msgs: list[dict]) -> None:
    for item, msg in zip(pending, tool_msgs):
        print(f"[run {run_id} turn {turn}] {item['name']}({item['args']}) → {msg['content']}")


def write_audit(run_id: str, messages: list[dict], mem: Memory) -> None:
    AUDIT_DIR.mkdir(exist_ok=True)
    (AUDIT_DIR / f"{run_id}.json").write_text(
        json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (AUDIT_DIR / f"{run_id}.memory.json").write_text(
        json.dumps(mem.dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_audit(run_id: str) -> tuple[list[dict], Memory]:
    messages = json.loads((AUDIT_DIR / f"{run_id}.json").read_text(encoding="utf-8"))
    mem = Memory.load(
        json.loads((AUDIT_DIR / f"{run_id}.memory.json").read_text(encoding="utf-8"))
    )
    return messages, mem
