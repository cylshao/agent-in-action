"""
09-agent-full-loop / config

一次 run 用到的常量：模型、密钥、时限、run_id、目标、system。按环境变量覆盖，
没设的从 .env 读。环境变量只在这一层读，代码里不再散落魔数。工具怎么分类是
toolset 的事，不在这儿。
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIT_DIR = HERE / "audit"
# 外部工具那台 Server 就是 mcp_tools.py 自己：被 import 时是 Client，被拉起时是 Server。
MCP_SERVER = HERE / "mcp_tools.py"


def load_dotenv() -> None:
    """把 .env 里的键读进 os.environ，从本章目录一路往上找到仓库根为止。

    已经在环境里的不覆盖：终端 export 过的、IDE 运行配置里填的，都压过文件。
    找到一份就停，所以本章目录的 .env 压过仓库根的那份。
    没引 python-dotenv，这点事不值得多一个依赖。
    """
    for base in (HERE, *HERE.parents):
        path = base / ".env"
        if path.is_file():
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                line = line.removeprefix("export ").lstrip()
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip("\"'")
            return
        if (base / ".git").exists():
            return  # 到仓库根还没有，就别再往上翻到 home 目录去了


load_dotenv()  # 必须在下面这些 getenv 之前

RUN_ID = os.getenv("RUN_ID", "run-tokyo")
MODEL = os.getenv("MODEL", "qwen/qwen-2.5-72b-instruct")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "your-api-key-here")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")

TOOL_TIMEOUT = float(os.getenv("TOOL_TIMEOUT", "8"))  # 本地工具
MCP_TIMEOUT = float(os.getenv("MCP_TIMEOUT", "25"))  # 外部工具：连进程 + 跑网络
MODEL_TIMEOUT = float(os.getenv("MODEL_TIMEOUT", "60"))

MAX_TURNS = int(os.getenv("MAX_TURNS", "12"))
TOKEN_BUDGET = int(os.getenv("TOKEN_BUDGET", "30000"))

GOAL = "东京 10 月 1 日住两晚。先查当天天气，下雨订 suite，否则订 standard。"

SYSTEM_PROMPT = (
    "你是出行助手。没有计划时，先调用 set_plan，不要先调业务工具。"
    "每轮只推进一个 status=pending 的步骤。"
    "步骤做成了就 mark_step(done)；做不到就 mark_step(failed)，再 set_plan。"
    "参数格式错、房型不在枚举里：不改计划，换参数重试。"
    "天气、房价、hotel_id 只能来自工具或记忆，不要编造。"
    "预订之前先给出终答，说清要订哪家、哪种房型，问一句确认，不要直接调 book_hotel。"
    "记忆里 confirmed 为 true 之后才调 book_hotel。订成之后给出终答，带上天气、酒店名、房型、总价。"
)

# 天气走 MCP 时多一步：先拿坐标。这句只在 --mcp 下加进 system。
MCP_PROMPT = "没有坐标时先 search_location，再 get_weather。经纬度不要编造。"