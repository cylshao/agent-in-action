# 09-agent-full-loop

前面几篇各在同一个 `while` 上加了一层：schema、记忆、计划、压缩、护栏、并发、超时、幂等键、审计、续跑、检查。这一篇不加新东西，只把它们装回同一个 `run`，跑完东京那次订房——从进循环，到终答问确认停住，再到确认之后续跑下单。

目标和前面几篇相同：东京 10 月 1 日住两晚，下雨订 suite，否则订 standard。

配套文章：《装回同一份循环：一次 run 从头到尾》。

## 跑起来

```bash
python agent.py             # 跑到终答问确认为止，messages 留在 audit/
python agent.py --confirm   # 读回同一个 run_id，接一条 user「确认」，续跑下单
python agent.py --mcp       # 天气改走 MCP：list_tools 拿 schema，dispatch 里 call_tool
python agent.py --check     # 拿审计里的 messages 跑轨迹级检查
python agent.py --selftest  # 不调模型，只跑护栏、并发回灌、幂等键、压缩这几段
```

前三条要模型，读 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL`：环境变量里没有的，`config.py` 会从本目录或仓库根目录的 `.env` 补上（`cp .env.example .env`）。后两条不调模型，也不需要装任何第三方包。

`--selftest` 是最快看清顺序的一条：同一批 `tool_calls` 里，`book_hotel` 未确认时回灌的是 error，另外两个只读工具照常并发，回灌顺序仍是 c1 → c2 → c3；同一轮里 `search_hotels` 搜到的 id 在订房执行前已经进了记忆；确认之后再订两次，`BOOKINGS` 里始终只有一单；压缩之后旧的 `tool` content 变成摘要，`tool_call_id` 一个不少。

加上 `--mcp` 再跑一次，能看到模型看见的工具列表换了：本地 `get_weather` 让位给 Server 上的 `search_location` 和 `get_weather`，其余几段输出一字不变。

## 一轮里的固定顺序

```
写记忆进 system → 压缩出副本 → 问模型
  ├ 没有 tool_calls → 给出终答（问确认则停住，等一条 user）
  └ 有 tool_calls  → 护栏筛一遍
                     → 只读工具并发、带超时；写操作单独走、带幂等键
                     → 按请求顺序回灌 → 轨迹日志 + 审计 → 下一轮
```

顺序不能换的三处：

- **压缩在问模型之前。** 压的是发出去的副本，不是 `messages` 本身。改了 `messages`，审计和续跑就只剩摘要。
- **护栏在执行之前。** 打转、未确认的写操作，看参数就能判。放到执行之后，房已经订了。
- **审计在回灌之后。** 一轮写一次，写的是配对完整的那一份。写在回灌之前，读回来续跑就是 400。

三个出口结束这次 `run`：给出终答、护栏终止、累计 token 或轮数到顶。停住等确认不是出口——`run_id`、`messages`、记忆都不变，等一条 user 再进来。

## 一层一个文件

```
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
```

分层只用两条规矩。

**依赖只朝下。** `tools` 和 `mcp_tools` 互不认识，中间隔着 `toolset`；`executor` 不认识 `guards`；`checks` 不认识 `config`，只认审计文件的格式和一份工具分类。

```
config      →
memory      →
tools       →
mcp_tools   → config
toolset     → tools mcp_tools memory
guards      → toolset
executor    → toolset config mcp_tools memory
audit       → config memory
loop        → audit config executor guards mcp_tools memory toolset
checks      → toolset
agent       → checks config loop mcp_tools selftest
```

**顺序只在一个地方。** 「护栏在执行之前」在 `loop.py` 里就是相邻的两行，不藏在谁的函数内部：

```python
allowed, blocked = screen(pending, mem.confirmed)
tool_msgs = tool_messages(pending, {**execute(mem, allowed, mcp), **blocked})
```

工具是纯函数，不碰记忆：要用当前计划、这次搜索的 `hotel_id`、`run_id`，由 `toolset` 显式当关键字参数传进去（模型的 schema 里没有这些字段，也填不了）；结果怎么进记忆只在 `memory.absorb` 一处。所以「白名单是从哪来的」只有一行代码可看，两次 `run` 也串不了记忆。

## 审计与续跑

每轮写一次，不等出口，按 `run_id` 落在 `audit/`（已 gitignore）：

- `audit/<run_id>.json` — 当时那份未压缩的 `messages`
- `audit/<run_id>.memory.json` — 当时那份记忆

`--confirm` 读的就是这两份：同一个 `run_id`、同一份 `messages` 和记忆，后面接一条 user「确认」再进循环。中间进程退出没关系。累计 token 跨续跑接着加，不清零。

`--check` 读的也是这份 `messages`，跑四条轨迹级检查：计划优先、跳步、参数来源、房型。不调模型——循环留下什么，它就判什么。所以审计留的必须是未压缩的那份：压缩副本里回灌原文没了，参数来源会对不上。

## 换成外部工具

`mcp_tools.py` 是一个文件两副身份：被 import 时是 Client，被 `Client` 拉起时跑的是同一个文件的 `__main__`，那半边挂着 `search_location` 和 `get_weather`，后端是 Open-Meteo。两副身份不共享内存，只隔着协议说话——所以「回灌不可信」在这里不是一句提醒，是进程边界本身：返回什么字段由 Server 说了算，进 `messages` 之前按 `RESULT_FIELDS` 裁一次。

默认那条线的 `get_weather` 是本地假数据，东京 10-01 固定下雨，「下雨订 suite」才每次都跑得出来。`--mcp` 换成真数据之后，Open-Meteo 的 forecast 只覆盖未来两周左右，`config.py` 里的 `GOAL` 用的 2026-10-01 会拿到 `error`（照常回灌，循环不受影响）。要真跑这条线，把 `GOAL` 里的日期换成近几天的。

## 对不上时

| 你看到什么 | 先动哪一层 |
|---|---|
| 累计 token 一路涨 | 压缩没接上，或压的是 `messages` 本身 |
| 模型编了一个 `hotel_id` | 记忆没写进 `system`，或压缩把回灌压没了 |
| 每轮重新想先做什么 | 计划没写进记忆 |
| 未确认就订了 | 护栏在执行之后，或只写在提示里 |
| 报 400 | 回灌没按请求顺序，或有一条没回灌，或审计写在回灌之前 |
| 一轮三个工具，总时间是三者之和 | 轮内并发没接上 |
| 工具挂住，循环不动 | 超时没包上 |
| 超时之后订了两单 | 幂等键没带，或超时后没先查 |
| 人回「确认」，记忆是空的 | 新开了 `run`，没读回审计 |
| 检查没法重跑 | 审计留的是压缩副本 |

一次只动一层。压缩的问题不要先加 `max_turns`，护栏的问题不要先改 schema，配对的问题不要先换模型。
