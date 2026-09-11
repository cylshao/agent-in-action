"""
09-agent-full-loop / mcp_tools

外部工具这一层。一个文件，两副身份，靠进程分开：

- 被 import 时是 Client（我们这头）：进循环之前 list_tools 拿 schema，执行时 call_tool。
- 被当脚本跑时是 Server（对端）：_serve() 把 search_location / get_weather 挂上 stdio。
  Client 连过来时拉起的就是 `python mcp_tools.py`，跑的是下面那个 __main__。

两边不共享内存，只隔着协议说话。所以：
- 名单外的工具不转成 schema，模型就看不见。
- 回灌不可信：返回什么字段由 Server 说了算，进 messages 之前按名单裁。
- 打开之后，本地同名的 get_weather 让位，循环和 messages 的写法都不动。

fastmcp 和 httpx 只在这两副身份真正用到时才 import：默认那条线不装也能跑。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from config import MCP_SERVER


class McpTools:
    """一台 MCP Server 的客户端。不开这一层时，整个对象就不存在。"""

    ALLOW = frozenset({"search_location", "get_weather"})
    RESULT_FIELDS: dict[str, tuple[str, ...]] = {
        # 下一步 get_weather 要用经纬度和时区，终答要报地名。
        "search_location": ("name", "latitude", "longitude", "timezone"),
        # 房型按 condition 填，终答报一下气温。原始 weathercode 用不上。
        "get_weather": ("date", "condition", "high"),
    }

    def __init__(self, server: Path = MCP_SERVER) -> None:
        self.server = server

    def handles(self, name: str) -> bool:
        return name in self.ALLOW

    # ---- 进循环之前问一次 ----

    def schemas(self) -> list[dict]:
        from fastmcp import Client

        async def go() -> list[dict]:
            async with Client(self.server) as mcp:
                listed = await mcp.list_tools()
                items = getattr(listed, "tools", listed)
                out, seen = [], set()
                for tool in items:
                    if tool.name not in self.ALLOW:
                        continue
                    out.append(_to_openai_tool(tool))
                    seen.add(tool.name)
                missing = self.ALLOW - seen
                if missing:
                    raise RuntimeError(f"MCP Server 缺少白名单里的工具：{sorted(missing)}")
                return out

        return asyncio.run(go())

    # ---- 执行 ----

    def call(self, name: str, args: dict) -> dict:
        """每次调用自己连一次 Server：轮内并发时各线程互不共用连接。"""
        from fastmcp import Client

        async def go() -> dict:
            async with Client(self.server) as mcp:
                return _payload(await mcp.call_tool(name, args))

        try:
            return self.trim(name, asyncio.run(go()))
        except Exception as e:
            return {"error": f"MCP call_tool 失败：{e}"}

    def trim(self, name: str, payload: dict) -> dict:
        keep = self.RESULT_FIELDS.get(name)
        if keep is None or "error" in payload:
            return payload
        trimmed = {k: v for k, v in payload.items() if k in keep}
        return trimmed or payload


def _to_openai_tool(tool: Any) -> dict:
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema or {"type": "object", "properties": {}},
        },
    }


def _payload(result: Any) -> dict:
    if getattr(result, "is_error", False):
        texts = [t for t in _texts(result) if t]
        return {"error": texts[0] if texts else "MCP Server 返回了 error"}
    for attr in ("data", "structured_content"):
        value = getattr(result, attr, None)
        if isinstance(value, dict):
            return value
    texts = [t for t in _texts(result) if t]
    if not texts:
        return {"error": "MCP Server 没有返回可解析的内容"}
    try:
        parsed = json.loads(texts[0])
    except json.JSONDecodeError:
        return {"error": texts[0][:200]}
    return parsed if isinstance(parsed, dict) else {"error": texts[0][:200]}


def _texts(result: Any) -> list[Any]:
    return [getattr(b, "text", None) for b in getattr(result, "content", []) or []]


# ---------- 对端：stdio MCP Server，后端是 Open-Meteo ----------

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Open-Meteo weathercode：0–3 晴或少云，45–48 雾，51 起是降水。
RAIN_CODES = set(range(51, 100))
# 「东京」在地理编码里会先命中中国地名，查日本要用 Tokyo。
CITY_QUERY = {"东京": "Tokyo", "東京": "Tokyo"}


def _serve() -> None:
    """只有被当脚本跑时才走到这里：这半边在另一个进程里，agent 碰不到。"""
    import httpx
    from fastmcp import FastMCP

    mcp = FastMCP("weather")
    http = httpx.Client(timeout=10.0)

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
    def get_weather(
        latitude: float, longitude: float, date: str, timezone: str = "auto"
    ) -> dict:
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
        # 这里回的字段比 Client 要的多：weathercode、经纬度都在。裁字段是那头的事。
        return {
            "latitude": lat,
            "longitude": lon,
            "date": date,
            "condition": "rain" if code in RAIN_CODES else "clear",
            "high": highs[0] if highs else None,
            "weathercode": code,
        }

    mcp.run()


if __name__ == "__main__":
    _serve()
