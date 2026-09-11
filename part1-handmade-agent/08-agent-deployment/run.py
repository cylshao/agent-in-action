"""
08-agent-deployment

循环不改。一轮里多个 tool_calls 同时执行，回灌仍按请求顺序配对。
工具超时回灌；写操作带幂等键，超时先查再订。
每次 run 一份 messages，按 run_id 每轮留下审计，确认之后续跑。

要点（与文章同一套叫法）：
- run = 一次目标从进循环到出口
- run_id = 这次 run 的标识
- 轮内并发 = 同一轮多个 tool_calls 同时执行
- 超时 = 单次工具超过时限
- 幂等键 = 这次订房的标识，重复提交只产生一次订单
- 轨迹日志 = 每轮打出 run_id、工具名、参数、回灌
- 审计 = 按 run_id 把 messages 留下，事后能再跑检查
- 续跑 = 带着已有的 run_id 和 messages 再进循环
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from pathlib import Path
from typing import Any, Callable

AUDIT_DIR = Path(__file__).resolve().parent / "audit"

# 订房这一头的状态。键是幂等键，重复提交只产生一次订单。
BOOKINGS: dict[str, dict[str, Any]] = {}


def idem_key(run_id: str, args: dict[str, Any]) -> str:
    return f"{run_id}:{args.get('hotel_id')}:{args.get('check_in')}"


def book_hotel(key: str, args: dict[str, Any], sleep: float = 0.0) -> dict[str, Any]:
    """先落单再睡：模拟「对端已经扣了库存，我们这头才超时」。"""
    if key in BOOKINGS:
        return {**BOOKINGS[key], "idempotent": True}
    booking = {
        "booking_id": f"BK-{len(BOOKINGS) + 1:03d}",
        "hotel_id": args.get("hotel_id"),
        "room_type": args.get("room_type"),
        "check_in": args.get("check_in"),
    }
    BOOKINGS[key] = booking
    if sleep:
        time.sleep(sleep)
    return booking


def find_booking(key: str) -> dict[str, Any] | None:
    return BOOKINGS.get(key)


def dispatch(name: str, args: dict[str, Any], sleep: float = 0.0) -> dict[str, Any]:
    if sleep:
        time.sleep(sleep)
    if name == "get_weather":
        return {"city": args.get("city"), "date": args.get("date"), "condition": "rain"}
    if name == "search_hotels":
        return {"hotels": [{"id": "HT-002", "name": "新宿京王广场"}]}
    return {"error": f"没有名为 {name} 的工具"}


def call_with_timeout(
    fn: Callable[..., dict],
    timeout: float,
    *a: Any,
    **kw: Any,
) -> dict[str, Any]:
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *a, **kw)
    try:
        return fut.result(timeout=timeout)
    except (TimeoutError, FuturesTimeout):
        return {"error": "超时"}
    finally:
        ex.shutdown(wait=False)


def run_turn_concurrent(
    pending: list[dict[str, Any]],
    timeout: float,
) -> tuple[list[dict[str, Any]], list[float]]:
    """同时执行，回灌仍按 pending 的顺序。finished 记录完成先后（秒）。"""
    t0 = time.perf_counter()
    finished_at: dict[str, float] = {}

    def one(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        result = call_with_timeout(
            dispatch,
            timeout,
            item["name"],
            item["args"],
            item.get("sleep", 0.0),
        )
        finished_at[item["id"]] = time.perf_counter() - t0
        return item["id"], result

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(pending) or 1) as ex:
        futs = [ex.submit(one, item) for item in pending]
        for fut in as_completed(futs):
            tid, result = fut.result()
            results[tid] = result

    tool_msgs = []
    for item in pending:
        tool_msgs.append(
            {
                "role": "tool",
                "tool_call_id": item["id"],
                "content": json.dumps(results[item["id"]], ensure_ascii=False),
            }
        )
    order = [finished_at[item["id"]] for item in pending]
    return tool_msgs, order


def log_turn(run_id: str, turn: int, pending: list[dict], tool_msgs: list[dict]) -> None:
    for item, msg in zip(pending, tool_msgs):
        print(
            f"[run {run_id} turn {turn}] {item['name']}({item['args']}) "
            f"→ {msg['content']}"
        )


def write_audit(run_id: str, messages: list[dict[str, Any]]) -> Path:
    """每轮写一次，不要等出口。进程中途没了，下一轮还能读回来。"""
    AUDIT_DIR.mkdir(exist_ok=True)
    path = AUDIT_DIR / f"{run_id}.json"
    path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_audit(run_id: str) -> list[dict[str, Any]]:
    path = AUDIT_DIR / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def assistant_call(tid: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tid,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        ],
    }


def tool_msg(tid: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tid,
        "content": json.dumps(payload, ensure_ascii=False),
    }


def demo_concurrent_and_timeout() -> list[dict[str, Any]]:
    run_id = "run-tokyo"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "规则 + 记忆"},
        {"role": "user", "content": "东京 10 月 1 日住两晚。下雨订 suite。"},
    ]
    pending = [
        {
            "id": "c1",
            "name": "search_hotels",
            "args": {"city": "东京", "check_in": "2026-10-01"},
            "sleep": 0.25,
        },
        {
            "id": "c2",
            "name": "get_weather",
            "args": {"city": "东京", "date": "2026-10-01"},
            "sleep": 0.05,
        },
        {
            "id": "c3",
            "name": "get_weather",
            "args": {"city": "大阪", "date": "2026-10-01"},
            "sleep": 0.80,
        },
    ]
    messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": p["id"],
                    "type": "function",
                    "function": {
                        "name": p["name"],
                        "arguments": json.dumps(p["args"], ensure_ascii=False),
                    },
                }
                for p in pending
            ],
        }
    )
    tool_msgs, finished = run_turn_concurrent(pending, timeout=0.40)
    messages.extend(tool_msgs)
    log_turn(run_id, 1, pending, tool_msgs)
    print(
        "完成先后（秒，按请求顺序列出）: "
        + ", ".join(f"{p['id']}={t:.2f}" for p, t in zip(pending, finished))
    )
    print("回灌顺序: " + " → ".join(m["tool_call_id"] for m in tool_msgs))
    path = write_audit(run_id, messages)
    print(f"审计: {path.name}，{len(messages)} 条 messages")
    return messages


def demo_two_runs() -> None:
    def one(run_id: str, city: str) -> str:
        messages = [
            {"role": "system", "content": "规则 + 记忆"},
            {"role": "user", "content": f"{city} 10 月 1 日住两晚"},
        ]
        pending = [
            {
                "id": "c1",
                "name": "get_weather",
                "args": {"city": city, "date": "2026-10-01"},
                "sleep": 0.05,
            }
        ]
        tool_msgs, _ = run_turn_concurrent(pending, timeout=1.0)
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": json.dumps(pending[0]["args"], ensure_ascii=False),
                        },
                    }
                ],
            }
        )
        messages.extend(tool_msgs)
        log_turn(run_id, 1, pending, tool_msgs)
        write_audit(run_id, messages)
        return run_id

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [
            ex.submit(one, "run-a", "东京"),
            ex.submit(one, "run-b", "大阪"),
        ]
        print("两次 run: " + ", ".join(f.result() for f in futs))


def demo_write_timeout() -> None:
    """写操作超时：回灌了，还不知道订没订上。先查这把幂等键，再决定要不要订。"""
    run_id = "run-book"
    args = {
        "hotel_id": "HT-002",
        "room_type": "suite",
        "check_in": "2026-10-01",
        "nights": 2,
    }
    key = idem_key(run_id, args)

    # 第 1 次：对端落了单，我们这头 0.4 秒就不等了。
    result = call_with_timeout(book_hotel, 0.40, key, args, 0.80)
    print(f"[run {run_id} turn 1] book_hotel({args}) → {result}")
    print(f"订单数：{len(BOOKINGS)}（对端其实已经落单）")

    if "error" in result:
        # 不要直接再 book_hotel。先查这把键。
        found = find_booking(key)
        print(f"超时后先查幂等键 {key} → {found}")
        if found:
            print("查到了：回灌已有订单，不再订")
        else:
            print("查不到：才视为没订成")

    # 就算下一轮模型还要订，同一把键也只会拿回那一单。
    again = book_hotel(key, args)
    print(f"再订一次 → {again}")
    print(f"订单数仍是 {len(BOOKINGS)}：幂等键挡住了双订")


def demo_confirm_and_resume() -> None:
    """确认之前停住，把 messages 留下；人回了确认，带同一份续跑。"""
    run_id = "run-confirm"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "规则 + 记忆"},
        {"role": "user", "content": "东京 10 月 1 日住两晚。下雨订 suite。"},
    ]

    # 第 1 轮：只读工具，回灌之后当场写审计。
    args = {"city": "东京", "check_in": "2026-10-01"}
    messages.append(assistant_call("c1", "search_hotels", args))
    messages.append(tool_msg("c1", dispatch("search_hotels", args)))
    log_turn(run_id, 1, [{"name": "search_hotels", "args": args}], messages[-1:])
    write_audit(run_id, messages)

    # 第 2 轮：终答问确认。循环在这里停住，不是出错，也不是终止。
    messages.append({"role": "assistant", "content": "即将预订新宿京王广场 suite，确认吗"})
    write_audit(run_id, messages)
    print(f"停住等确认：审计里已有 {len(messages)} 条 messages")

    # —— 换一个进程也一样：只认 run_id ——
    resumed = read_audit(run_id)
    resumed.append({"role": "user", "content": "确认"})
    confirmed = resumed[-1]["role"] == "user"

    book_args = {
        "hotel_id": "HT-002",
        "room_type": "suite",
        "check_in": "2026-10-01",
        "nights": 2,
    }
    key = idem_key(run_id, book_args)
    payload = (
        book_hotel(key, book_args)
        if confirmed
        else {"error": "写操作未确认，不执行 book_hotel"}
    )
    resumed.append(assistant_call("c2", "book_hotel", book_args))
    resumed.append(tool_msg("c2", payload))
    log_turn(run_id, 3, [{"name": "book_hotel", "args": book_args}], resumed[-1:])
    write_audit(run_id, resumed)
    print(f"续跑之后：{len(resumed)} 条 messages，同一个 run_id={run_id}")


def main() -> None:
    print("一轮三个 tool_calls：c2 先完成，c3 超时，回灌仍是 c1 → c2 → c3")
    demo_concurrent_and_timeout()
    print()
    print("写操作超时：先查幂等键，再决定要不要订")
    demo_write_timeout()
    print()
    print("确认之前停住，人回了确认带同一份 messages 续跑")
    demo_confirm_and_resume()
    print()
    print("两次 run 同时跑，各写各的审计")
    demo_two_runs()


if __name__ == "__main__":
    main()
