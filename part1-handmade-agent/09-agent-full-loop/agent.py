"""
09-agent-full-loop

把前面几篇各加的一层，装回同一个 run。循环还是那个循环：
带 tools 问模型 → 有 tool_calls 就执行 → 回灌 → 再问，直到出口。

这一篇的代码不引别的目录，一层一个文件：

    agent.py        入口：解析参数，决定跑哪条路
    └─ loop.py      一轮的顺序（唯一知道「先后」的地方）
       ├─ memory.py   记忆、absorb、压缩
       ├─ toolset.py  模型看见什么 + 名字归谁执行
       │  ├─ tools.py      本地实现（纯函数，不碰记忆）
       │  └─ mcp_tools.py  外部实现（一个文件两副身份：Client / Server）
       ├─ guards.py   执行之前的筛选
       ├─ executor.py 并发、超时、写操作、按请求顺序回灌
       └─ audit.py    执行之后的留痕

    config.py       常量、密钥、时限、system：谁都能读，它不读谁
    checks.py       不是层：只读审计文件的另一个程序
    selftest.py     不是层：不调模型的几段演示

依赖只朝下：tools 和 mcp_tools 互不认识，executor 不认识 guards，checks 不认识 config。

用法：
    python agent.py               跑到问确认为止，messages 留在 audit/
    python agent.py --confirm     读回同一个 run_id，接一条 user「确认」，续跑
    python agent.py --mcp         天气改走 MCP：list_tools 拿 schema，dispatch 里 call_tool
    python agent.py --check       拿审计里的 messages 跑轨迹级检查，不调模型
    python agent.py --selftest    只跑纯函数那几段（护栏、压缩、幂等键、并发回灌），不调模型

默认的 get_weather 是本地假数据，东京 10-01 固定下雨，这条线才复现得了。
--mcp 把它换成 MCP Server 上的 search_location / get_weather：真取 Open-Meteo，
循环、messages、记忆、护栏都不动，只有 dispatch 多一条分流、回灌之前多一次裁字段。
"""

from __future__ import annotations

import sys

from checks import check_audit
from config import AUDIT_DIR, RUN_ID
from loop import run
from mcp_tools import McpTools
from selftest import selftest

FLAGS = {"--mcp", "--confirm", "--selftest", "--check"}


def main(argv: list[str]) -> None:
    mcp = McpTools() if "--mcp" in argv else None
    if "--selftest" in argv:
        selftest(mcp)
    elif "--check" in argv:
        paths = [a for a in argv if a not in FLAGS]
        check_audit(paths or [str(AUDIT_DIR / f"{RUN_ID}.json")])
    else:
        print(run(resume="--confirm" in argv, mcp=mcp))


if __name__ == "__main__":
    main(sys.argv[1:])
