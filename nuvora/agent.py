"""NUVORA Agent 组装：LangGraph create_agent + SqliteSaver 检查点。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import SYSTEM_PROMPT_TEMPLATE, APP_NAME, TAGLINE, __version__
from .config import Config, WORKSPACE_DIR
from .memory import LongTermMemory
from .tools import build_tools

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def open_checkpointer(db_path: Path):
    """打开一个 SqliteSaver（会话检查点/短期记忆）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)


def build_system_prompt(cfg: Config, memory: LongTermMemory | None) -> str:
    now = datetime.now().astimezone()
    memory_block = memory.memory_block() if memory is not None else "（长期记忆未启用）"
    return SYSTEM_PROMPT_TEMPLATE.format(
        app_name=APP_NAME,
        tagline=TAGLINE,
        version=__version__,
        today=now.strftime("%Y-%m-%d"),
        weekday=WEEKDAY_CN[now.weekday()],
        now=now.strftime("%H:%M"),
        memory_block=memory_block,
    )


def build_agent(cfg: Config, model, memory: LongTermMemory | None, checkpointer):
    """组装 agent 图。兼容 create_agent 不同版本的 system_prompt/prompt 参数名。"""
    from langchain.agents import create_agent

    tools = build_tools(cfg, memory if cfg.memory.enabled else None)
    system_prompt = build_system_prompt(cfg, memory)
    kwargs = dict(model=model, tools=tools, checkpointer=checkpointer)
    try:
        return create_agent(system_prompt=system_prompt, **kwargs)
    except TypeError:
        return create_agent(prompt=system_prompt, **kwargs)
