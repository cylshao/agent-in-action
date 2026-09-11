# part2-agent-frameworks

《Agent 框架系列》的配套代码。先只开 LangGraph 这一条，一个目录一层，对着手搓系列往上加。相邻章用 Compare Files 看 diff。

`.env` 和 `requirements.txt` 跟 part1 共用仓库根那一份。读环境和接模型在本目录的 `env.py`，各层 `from env import chat_model`，不要再抄。

```
part2-agent-frameworks/
├─ env.py                 读 .env，给出 init_chat_model
└─ langgraph/
   └─ 01-min-loop/        把手搓那个 while 画成图，先不落盘
```

| 目录 | 这一层 | 对上 part1 |
| --- | --- | --- |
| `langgraph/01-min-loop/` | 两个节点：问模型、执行回灌 | `01-agent-from-scratch` |

## 01-min-loop

把手搓那个 `while` 画成图。`search_location` + `get_current_weather`，走 Open-Meteo，两个节点，不落盘、不停住。配套文章：《LangGraph：用 StateGraph 把手搓那个 while 画成图》。

```bash
cd langgraph/01-min-loop
python agent.py
```

过关：轨迹里先 `search_location`，再 `get_current_weather`，终答里的温度对得上后一次回灌。
