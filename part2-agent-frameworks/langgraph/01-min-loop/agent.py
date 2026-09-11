"""
01-min-loop

把手搓那个 while 画成图：agent 问模型，tools 执行并回灌。
两个天气工具，Open-Meteo。不落盘，不停住。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

import httpx
from langchain.messages import HumanMessage, SystemMessage
from langchain.tools import tool
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from env import chat_model

# 模型只看见 SYSTEM 和 @tool 的 docstring，看不见函数体。
SYSTEM = (
    "只能通过工具获取事实，不要编温度或坐标。"
    "没有坐标时先 search_location，再 get_current_weather。"
    "拿到数据后给出终答，带上温度、体感和湿度。"
)
GOAL = "西安现在天气怎么样？"
http = httpx.Client(timeout=10.0)


def _get(url: str, params: dict) -> dict:
    """上游失败回灌 error，对照手搓里 dispatch 接住 HTTPError。"""
    try:
        r = http.get(url, params=params)
        r.raise_for_status()
    except httpx.HTTPError as e:
        return {"error": f"上游请求失败: {e}"}
    return r.json()


# @tool 对照手搓里手写的 TOOL_SCHEMAS：schema 从函数签名和 docstring 来。
@tool
def search_location(name: str) -> dict:
    """按地名搜索地理坐标。当你只有城市名、需要经纬度时先调它。地点如 Tokyo、北京。"""
    data = _get(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": name, "count": 1, "language": "zh", "format": "json"},
    )
    if "error" in data:
        return data
    results = data.get("results") or []
    if not results:
        return {"error": f"找不到地点 {name}，请检查拼写"}
    top = results[0]
    return {
        "name": top.get("name"),
        "country": top.get("country"),
        "latitude": top.get("latitude"),
        "longitude": top.get("longitude"),
    }


@tool
def get_current_weather(latitude: float, longitude: float) -> dict:
    """按经纬度获取当前天气。没有坐标时先用 search_location 拿到。"""
    data = _get(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m",
        },
    )
    if "error" in data:
        return data
    cur = data.get("current") or {}
    return {
        "temperature": cur.get("temperature_2m"),
        "apparent_temperature": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind_speed": cur.get("wind_speed_10m"),
    }


class State(TypedDict):
    """图上节点之间传的那份状态。

    这篇只有 messages。add_messages 对照手搓里的 messages.append：
    节点只返回新消息，旧的不会被盖掉。
    """

    messages: Annotated[list, add_messages]


# 顺序故意把天气放前面。模型应靠 description 先搜坐标再查天气，而不是按数组下标。
TOOLS = [get_current_weather, search_location]
bound = chat_model().bind_tools(TOOLS)


def agent(state: State) -> dict:
    """对照手搓里「带 tools 问模型」那一行。

    有 tool_calls 就停在这里，自己不执行。
    """
    return {"messages": [bound.invoke(state["messages"])]}


# StateGraph 用来把那两件事画成图，按「问 → 执行 → 再问」串起来，对照手搓里写在 while / for 里的顺序。
builder = StateGraph(State)  # 就是先立一张空白图，并规定节点之间传这种 State

# add_node 是在图上创建一个节点，指定这个节点执行哪个函数
builder.add_node("agent", agent)  # 创建节点名叫 agent，跑问模型那个函数。对照手搓里 create(..., tools=...)
builder.add_node("tools", ToolNode(TOOLS))  # 创建节点名叫 tools，跑执行并按 id 回灌，对照手搓里的 dispatch

# add_edge 是在两个节点之间的连线：START 不是你写的节点，是图的入口
builder.add_edge(START, "agent")  # 一开始一定先问模型

# add_conditional_edges 是一条活线，下一站由函数当场决定
# agent 问完模型之后，用 tools_condition 看出这轮有没有 tool_calls
builder.add_conditional_edges("agent", tools_condition)  # 有 tool_calls → tools；没有 → END，对照手搓里 if not tool_calls: return

# add_edge 是一条死线：tools 做完，一定回到 agent，对照手搓里执行完继续 for
builder.add_edge("tools", "agent")

# compile() 把上面的节点和连线收成一张能跑的图
graph = builder.compile()


def main() -> None:
    """对照手搓里的 run()：把初始 messages 丢进图，从 START 走到 END。

    每到一个节点就跑一次，tools 回来再进 agent，直到没有 tool_calls。
    返回的是走完之后的整份 State，轨迹都在 result["messages"] 里。
    """
    result = graph.invoke(
        {
            "messages": [
                SystemMessage(content=SYSTEM),
                HumanMessage(content=GOAL),
            ]
        }
    )
    print("—— 轨迹 ——")
    for m in result["messages"]:
        calls = getattr(m, "tool_calls", None)
        if calls:
            print(f"{type(m).__name__:20} 要调 {[c['name'] for c in calls]}")
        else:
            print(f"{type(m).__name__:20} {m.content}")


if __name__ == "__main__":
    main()
