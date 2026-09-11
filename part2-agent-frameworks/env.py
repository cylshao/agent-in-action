"""
part2 共用的环境：读仓库根的 .env，再给出 init_chat_model。

part1 直接用 openai SDK。这边换 init_chat_model，仍连同一份
OPENAI_API_KEY / OPENAI_BASE_URL / MODEL。环境变量压过文件。
"""

from __future__ import annotations

import os
from pathlib import Path

from langchain.chat_models import init_chat_model


def load_dotenv() -> None:
    # 仓库根那一份，不写死往上几层。已经在环境里的不覆盖。
    root = next(
        (p for p in Path(__file__).resolve().parents if (p / ".git").exists()),
        Path(__file__).resolve().parent,
    )
    path = root / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


def chat_model():
    """接上 .env 里那个 OpenAI 兼容端点。"""
    load_dotenv()
    return init_chat_model(
        os.getenv("MODEL", "qwen/qwen-2.5-72b-instruct"),
        model_provider="openai",
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
    )
