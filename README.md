# Agent In Action

博客各个 Agent 系列的配套代码。一个系列一个目录，一篇文章一个子目录。

```
agent-in-action/
├─ part1-handmade-agent/     《手搓 Agent 系列》九篇：不用框架，从一个 while 循环长到能上线
│  ├─ 01-agent-from-scratch/
│  ├─ …
│  └─ 09-agent-full-loop/
├─ part2-agent-frameworks/   《Agent 框架系列》：一个目录一层，公共函数在目录根
├─ requirements.txt          整个工程共用一份
└─ .env.example              复制成 .env 填 key
```

再往后是企业级 Agent 开发，到时候新开 `part3-`。

## 环境准备

Python 3.12。part1 三个依赖，part2 再加 langchain / langgraph 那几个，整份工程共用一个 `requirements.txt`：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

调模型的几篇读三个环境变量：

| 变量 | 缺省值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 无，必填 | |
| `OPENAI_BASE_URL` | `https://openrouter.ai/api/v1` | 换任何 OpenAI 兼容端点 |
| `MODEL` | `qwen/qwen-2.5-72b-instruct` | |

写进仓库根的 `.env` 就不用每开一个终端 export 一次，从 IDE 里跑也读得到：

```bash
cp .env.example .env   # 填上 key，.env 已在 .gitignore 里
```

环境变量优先于 `.env`：终端 export 过的、IDE 运行配置里填的，都压过文件。part1 的 `01`–`05`、`09`，以及 part2 各层，都读这份文件。part2 的加载和接模型在 `part2-agent-frameworks/env.py`，各层 `from env import chat_model`。

不调模型的那几篇不需要 key，标准库就能跑：part1 的 `06`、`07`、`08`，以及 `09` 的 `--selftest` / `--check`。

## part1-handmade-agent · 手搓 Agent 系列

循环从第一篇起就没怎么变过，每篇只往它外面加一层。

| 目录 | 这一篇加的那层 |
| --- | --- |
| `01-agent-from-scratch/` | 剥掉框架，纯手工实现一个基于工具调用循环的 Agent |
| `02-agent-tool-design/` | 换订房工具，演示 schema、参数校验与写操作白名单 |
| `03-agent-context-memory/` | 压缩旧的 tool 返回，把目标和结论放到循环外的记忆 |
| `04-agent-planning/` | 记忆里加计划，用 `set_plan` / `mark_step` 按步骤推进 |
| `05-agent-mcp-tools/` | 天气工具搬到 MCP Server，用 `list_tools` / `call_tool` 接入；回灌按字段名单裁 |
| `06-agent-eval/` | 从 messages 抽出轨迹，用代码检查跳步、参数来源、房型 |
| `07-agent-reliability-cost/` | 护栏拦打转和未确认的写操作；累计 token 按每轮重发加总 |
| `08-agent-deployment/` | 轮内并发、工具超时回灌、写操作带幂等键、按 run_id 留审计并续跑 |
| `09-agent-full-loop/` | 前面各层装回同一个 run，一层一个文件，依赖只朝下 |

`01`–`05` 进目录 `python agent.py` 就跑。带参数的有两处：

```bash
cd part1-handmade-agent/06-agent-eval
python eval.py                # 跑五条对照轨迹
python eval.py run.json       # 把真跑留下的 messages 丢进同一套检查

cd part1-handmade-agent/09-agent-full-loop
python agent.py               # 跑到终答问确认为止
python agent.py --confirm     # 读回同一个 run_id，续跑下单
python agent.py --mcp         # 天气改走 MCP
python agent.py --check       # 拿审计跑轨迹级检查，不调模型
python agent.py --selftest    # 只跑那几段纯函数，不调模型
```

`09` 的分层、一轮里的固定顺序、审计与续跑，见 [该目录的 README](part1-handmade-agent/09-agent-full-loop/README.md)。

### 怎么比对相邻章

`01`–`05` 的 `agent.py` 按同一套骨架往下叠：

```
imports → 读 .env → SYSTEM_PROMPT / GOAL → 工具 → REGISTRY / TOOL_SCHEMAS → dispatch → run
```

上一章已经有的循环、校验、压缩、记忆，后面不再改写法，只加该章的一层。所以在 PyCharm 里选两个 `agent.py` 右键 **Compare Files**（或用 Compare Directories 对比两个章节目录），diff 里剩下的几乎就是这一篇新加的内容，不必整文件重读。

`06`–`08` 不跑循环，比对的是 `eval.py` / `cost.py` / `run.py`。`09` 把前面各层拆成了文件，不再和 `05/agent.py` 整文件对。

## part2-agent-frameworks · Agent 框架系列

先只开 LangGraph。一个目录一层，对着手搓往上加。读环境和接模型在 `env.py`。

| 目录 | 这一层 | 对上 part1 |
| --- | --- | --- |
| `langgraph/01-min-loop/` | 两个节点：问模型、执行回灌 | `01-agent-from-scratch` |

```bash
cd part2-agent-frameworks/langgraph/01-min-loop
python agent.py
```

过关：轨迹里先 `search_location`，再 `get_current_weather`，终答里的温度对得上后一次回灌。
