# part2-agent-frameworks

《Agent 框架系列》的配套代码。先只开 LangGraph 这一条，一个目录一层，对着手搓系列往上加。相邻章用 Compare Files 看 diff。

`.env` 和 `requirements.txt` 跟 part1 共用仓库根那一份。读环境和接模型在本目录的 `env.py`，各层 `from env import chat_model`，不要再抄。

```
part2-agent-frameworks/
├─ env.py                 读 .env，给出 init_chat_model
└─ langgraph/
   └─ 01-min-loop/        把手搓那个 while 画成图
```

| 目录 | 这一层 | 对上 part1 |
| --- | --- | --- |
| `langgraph/01-min-loop/` | 两个节点；天气 + 订房挂同一张图 | `01-agent-from-scratch` + `02-agent-tool-design` |

## 01-min-loop

把手搓那个 `while` 画成图。天气走 Open-Meteo，订房用本地假库存，`hotel_id` 白名单在函数体里。一次 `invoke` 先看天气再订房。不落盘、不停住。配套文章：《LangGraph：用 StateGraph 把手搓那个 while 画成图》。

```bash
cd langgraph/01-min-loop
python agent.py
```

过关：先搜地点再查天气，再 `search_hotels` / `book_hotel`。终答温度对得上天气回灌，`hotel_id` 来自搜索结果，房型是 `suite`。
