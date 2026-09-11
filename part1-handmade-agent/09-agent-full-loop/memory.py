"""
09-agent-full-loop / memory

模型下一轮看得见什么，由这一层决定：

- 记忆：循环外的一份状态，每轮写进 system。计划也在里面。
- absorb：工具返回的东西怎么进记忆，只在这一处。工具自己不写记忆，
  所以「hotel_id 白名单是从哪来的」只有一行代码可看。
- 压缩：发出去之前做一份副本，只改旧 tool 的 content，不删消息。

压缩不动 messages 本身——审计和续跑要的是未压缩的那份。
"""

from __future__ import annotations

import json
from typing import Any, Optional


class Memory:
    def __init__(self, goal: str, run_id: str) -> None:
        self.goal = goal
        self.run_id = run_id
        self.plan: list[dict[str, Any]] = []
        self.location: Optional[dict[str, Any]] = None
        self.weather: Optional[dict[str, Any]] = None
        self.hotel_ids: list[str] = []
        self.hotels: list[dict[str, Any]] = []
        self.rejected: list[str] = []
        self.confirmed = False
        self.booked: Optional[dict[str, Any]] = None
        self.used_tokens = 0

    # ---- 工具返回进记忆 ----

    def absorb(self, name: str, result: dict[str, Any]) -> None:
        """回灌进 messages 的同时，这一轮学到的东西也落进记忆。"""
        if "error" in result:
            if name == "book_hotel":
                self.rejected.append(str(result["error"]))
            return
        if name in ("set_plan", "mark_step"):
            self.plan = result["plan"]
        elif name == "get_weather":
            self.weather = result
        elif name == "search_location":
            self.location = result
        elif name == "search_hotels":
            self.hotels = result["hotels"]
            self.hotel_ids = [h["id"] for h in result["hotels"]]
        elif name == "book_hotel":
            self.booked = {
                "booking_id": result.get("booking_id"),
                "hotel_name": result.get("hotel_name"),
                "total": result.get("total"),
            }

    # ---- 写进 system / 存进审计 ----

    def as_prompt(self) -> str:
        # confirmed 给模型看，是为了让它知道现在能不能订；hotel_ids 白名单在代码里另查。
        payload = {
            "goal": self.goal,
            "plan": self.plan,
            "weather": self.weather,
            "hotel_ids": self.hotel_ids,
            "hotels": self.hotels,
            "rejected": self.rejected,
            "confirmed": self.confirmed,
            "booked": self.booked,
        }
        if self.location is not None:
            payload["location"] = self.location
        return "记忆：\n" + json.dumps(payload, ensure_ascii=False)

    def dump(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def load(cls, data: dict[str, Any]) -> "Memory":
        mem = cls(data["goal"], data["run_id"])
        mem.__dict__.update(data)
        return mem


def compact_messages(messages: list[dict], keep_rounds: int = 1) -> list[dict]:
    """旧的 tool 换成摘要，配对不动：assistant.tool_calls 和 tool 仍是一对一。"""
    rounds: list[list[int]] = []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            rounds.append([i])
        elif m.get("role") == "tool" and rounds:
            rounds[-1].append(i)
    keep_from = len(rounds) - keep_rounds
    out = [dict(m) for m in messages]
    for r, idxs in enumerate(rounds):
        if r >= keep_from:
            continue
        for i in idxs[1:]:
            try:
                payload = json.loads(out[i]["content"])
            except (json.JSONDecodeError, TypeError):
                continue
            summary: dict[str, Any] = {"_compacted": True}
            if "error" in payload:
                summary["error"] = str(payload["error"])[:60]
            elif "hotels" in payload:
                summary["hotel_ids"] = [h["id"] for h in payload["hotels"]]
            elif "condition" in payload:
                summary["condition"] = payload["condition"]
            out[i]["content"] = json.dumps(summary, ensure_ascii=False)
    return out
