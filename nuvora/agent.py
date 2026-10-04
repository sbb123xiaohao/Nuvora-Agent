"""NUVORA Agent 组装：LangGraph create_agent + SqliteSaver 检查点。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import SYSTEM_PROMPT_TEMPLATE, APP_NAME, TAGLINE, __version__
from .config import Config
from .memory import LongTermMemory
from .tools import build_tools

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def open_checkpointer(db_path: Path):
    """打开一个 SqliteSaver（会话检查点/短期记忆）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return SqliteSaver(conn)
    except BaseException:
        conn.close()
        raise


def list_thread_ids(checkpointer) -> list[str]:
    """按最新检查点列出全部会话，不反序列化每一条历史消息。"""
    with checkpointer.cursor(transaction=False) as cursor:
        rows = cursor.execute(
            "SELECT thread_id FROM checkpoints WHERE checkpoint_ns = '' "
            "GROUP BY thread_id ORDER BY MAX(checkpoint_id) DESC"
        ).fetchall()
    return [row[0] for row in rows]


def build_system_prompt(cfg: Config, memory: LongTermMemory | None, *, tools=None) -> str:
    now = datetime.now().astimezone()
    memory_block = memory.memory_block() if memory is not None and cfg.memory.enabled else "（长期记忆未启用）"
    if tools is None:
        tools = build_tools(cfg, memory if cfg.memory.enabled else None)
    available = [tool.name for tool in tools]
    return SYSTEM_PROMPT_TEMPLATE.format(
        app_name=APP_NAME,
        tagline=TAGLINE,
        version=__version__,
        today=now.strftime("%Y-%m-%d"),
        weekday=WEEKDAY_CN[now.weekday()],
        now=now.strftime("%H:%M"),
        memory_block=memory_block,
        available_tools=", ".join(available),
    )


def build_agent(cfg: Config, model, memory: LongTermMemory | None, checkpointer, *, workspace=None):
    """按已验证的 LangChain 版本组装 agent 图。"""
    from langchain.agents import create_agent

    tools = build_tools(cfg, memory if cfg.memory.enabled else None, workspace)
    system_prompt = build_system_prompt(cfg, memory, tools=tools)
    return create_agent(model=model, tools=tools, system_prompt=system_prompt, checkpointer=checkpointer)
