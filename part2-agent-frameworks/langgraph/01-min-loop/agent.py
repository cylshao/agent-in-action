"""
01-min-loop

把手搓那个 while 画成图：agent 问模型，tools 执行并回灌。
天气走 Open-Meteo，订房用本地假库存。不落盘，不停住。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, TypedDict

import httpx
from langchain.messages import HumanMessage, SystemMessage
from langchain.tools import tool
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from env import chat_model

# 模型只看见 SYSTEM 和 @tool 的 docstring / 类型，看不见函数体。
SYSTEM = (
    "只能通过工具获取事实，不要编温度、坐标、房价或 hotel_id。"
    "没有坐标时先 search_location，再 get_current_weather。"
    "订房没有 hotel_id 时先 search_hotels，再 book_hotel。"
    "有依赖的下一步，等回灌之后再调，不要同一轮一起调。"
    "用户只是询问有没有房或多少钱时不要下单。"
    "给出终答，带上天气、酒店名、房型、入住日期和总价。"
)
GOAL = "西安现在天气怎么样？再帮我订一间豪华套房，2026 年 10 月 1 日住两晚。"
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


class Hotel(TypedDict):
    id: str
    name: str
    city: str
    room_types: list[str]
    price_per_night: dict[str, int]


# 本地假库存。真实项目里这是下游服务；这里用来演示校验，不打外部订房接口。
INVENTORY: list[Hotel] = [
    {
        "id": "HT-001",
        "name": "西安钟楼饭店",
        "city": "西安",
        "room_types": ["standard", "deluxe"],
        "price_per_night": {"standard": 180, "deluxe": 260},
    },
    {
        "id": "HT-002",
        "name": "西安香格里拉",
        "city": "西安",
        "room_types": ["standard", "deluxe", "suite"],
        "price_per_night": {"standard": 150, "deluxe": 220, "suite": 400},
    },
    {
        "id": "HT-003",
        "name": "回民街文化酒店",
        "city": "西安",
        "room_types": ["standard"],
        "price_per_night": {"standard": 110},
    },
]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 本次 invoke 里搜索过的 hotel_id。book_hotel 只接受名单内的 id。
seen_hotel_ids: set[str] = set()


def _check_in(value: str) -> str | dict:
    if not DATE_RE.match(value):
        return {"error": "check_in 必须是 YYYY-MM-DD，例如 2026-10-01"}
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return {"error": f"check_in 不是合法日期: {value}"}
    return value


# Literal 对照 enum；docstring 对照 description。
@tool
def search_hotels(city: str, check_in: str) -> dict:
    """按城市和入住日期搜索可订酒店，返回 id、房型和每晚价格。
    用户只给了城市名、还没有 hotel_id 时先调它。不要用它下单。
    check_in 格式 YYYY-MM-DD，例如 2026-10-01。"""
    checked = _check_in(check_in)
    if isinstance(checked, dict):
        return checked
    hits: list[Hotel] = []
    for h in INVENTORY:
        if city in h["city"] or h["city"] in city:
            hotel_id = h["id"]
            seen_hotel_ids.add(hotel_id)
            hits.append(
                {
                    "id": hotel_id,
                    "name": h["name"],
                    "city": h["city"],
                    "room_types": h["room_types"],
                    "price_per_night": h["price_per_night"],
                }
            )
    if not hits:
        return {"error": f"没有找到城市 {city} 的可订酒店，请换一个城市名"}
    return {"check_in": checked, "hotels": hits}


@tool
def book_hotel(
    hotel_id: str,
    room_type: Literal["standard", "deluxe", "suite"],
    check_in: str,
    nights: int,
) -> dict:
    """用 search_hotels 返回的 hotel_id 预订房间。
    没有 hotel_id 时不要编一个，先搜。
    用户只是询问有没有房或多少钱时不要调用。
    hotel_id 形如 HT-001，不是酒店名。nights 是 1 到 14 的整数。"""
    # 白名单在函数体里，schema 里没有这一项。
    if hotel_id not in seen_hotel_ids:
        return {
            "error": (
                f"hotel_id {hotel_id} 不在本次搜索结果里。"
                f"请先 search_hotels，可用 id：{sorted(seen_hotel_ids) or '（还没有，先搜）'}"
            )
        }
    hotel = next((h for h in INVENTORY if h["id"] == hotel_id), None)
    if hotel is None:
        return {"error": f"库存里没有 {hotel_id}"}
    if room_type not in hotel["room_types"]:
        return {
            "error": f"{hotel['name']} 没有 {room_type}，可选：{hotel['room_types']}"
        }
    checked = _check_in(check_in)
    if isinstance(checked, dict):
        return checked
    if nights < 1 or nights > 14:
        return {"error": "nights 必须是 1 到 14 的整数"}
    unit = hotel["price_per_night"][room_type]
    return {
        "status": "ok",
        "hotel_id": hotel_id,
        "hotel_name": hotel["name"],
        "room_type": room_type,
        "check_in": checked,
        "nights": nights,
        "total": unit * nights,
        "currency": "RMB",
    }


class State(TypedDict):
    """图上节点之间传的那份状态。

    这篇只有 messages。add_messages 对照手搓里的 messages.append：
    节点只返回新消息，旧的不会被盖掉。
    """

    messages: Annotated[list, add_messages]


# 顺序故意把执行类放前面。模型应靠 docstring 先搜再查、先搜后订，而不是按数组下标。
TOOLS = [book_hotel, get_current_weather, search_hotels, search_location]
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
    seen_hotel_ids.clear()
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
