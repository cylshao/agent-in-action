"""
05-agent-mcp-tools / mcp_server.py

stdio MCP Server。外部工具：search_location、get_weather，后端是 Open-Meteo。
agent 进程用 list_tools / call_tool 连过来，不把这两个函数写进 REGISTRY。
get_weather 只收坐标和日期，不查城市。
"""

from __future__ import annotations

import re

import httpx
from fastmcp import FastMCP

mcp = FastMCP("weather")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
http = httpx.Client(timeout=10.0)

# Open-Meteo weathercode：0–3 晴或少云，45–48 雾，51 起是降水。
RAIN_CODES = set(range(51, 100))
# 「东京」在地理编码里会先命中中国地名，查日本要用 Tokyo。
CITY_QUERY = {"东京": "Tokyo", "東京": "Tokyo"}


@mcp.tool
def search_location(city: str) -> dict:
    """按城市名查经纬度和时区。没有坐标时先调它，再 get_weather。"""
    query = CITY_QUERY.get(city, city)
    r = http.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": query, "count": 5, "language": "zh", "format": "json"},
    )
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        return {"error": f"找不到地点 {city}，换城市名重试"}
    jp = [x for x in results if x.get("country_code") == "JP"]
    top = (jp or results)[0]
    return {
        "name": top.get("name") or city,
        "latitude": top.get("latitude"),
        "longitude": top.get("longitude"),
        "timezone": top.get("timezone") or "Asia/Tokyo",
    }


@mcp.tool
def get_weather(latitude: float, longitude: float, date: str, timezone: str = "auto") -> dict:
    """按经纬度和日期查天气，返回 rain 或 clear。没有坐标时先 search_location。
    经纬度只能来自 search_location 或记忆，不要编造。
    日期格式不对就换参数重试；该日期确实没有数据再 mark_step(failed) 后 set_plan。"""
    if not DATE_RE.match(date):
        return {"error": "date 必须是 YYYY-MM-DD，例如 2026-10-01"}
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return {"error": "latitude / longitude 必须是数字，先 search_location"}
    r = http.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "weathercode,temperature_2m_max",
            "start_date": date,
            "end_date": date,
            "timezone": timezone or "auto",
        },
    )
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        return {"error": body.get("reason") or f"没有 {lat},{lon} {date} 的天气"}
    daily = body.get("daily") or {}
    codes = daily.get("weathercode") or []
    highs = daily.get("temperature_2m_max") or []
    if not codes or codes[0] is None:
        return {"error": f"没有 {lat},{lon} {date} 的天气"}
    code = int(codes[0])
    return {
        "latitude": lat,
        "longitude": lon,
        "date": date,
        "condition": "rain" if code in RAIN_CODES else "clear",
        "high": highs[0] if highs else None,
        "weathercode": code,
    }


if __name__ == "__main__":
    mcp.run()
