"""NUVORA 长期记忆：SQLite 存储的用户事实/偏好，配 remember / recall / forget 工具。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


class LongTermMemory:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                content    TEXT NOT NULL,
                tags       TEXT NOT NULL DEFAULT '',
                thread_id  TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self.conn.commit()

    def add(self, content: str, tags: str = "", thread_id: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO memories (created_at, content, tags, thread_id) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), content.strip(), tags.strip(), thread_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def search(self, query: str, limit: int = 5) -> list[tuple[int, str, str, str]]:
        """按关键词在 content 和 tags 里做 LIKE 检索，新的优先。"""
        like = f"%{query.strip()}%"
        cur = self.conn.execute(
            """SELECT id, created_at, content, tags FROM memories
               WHERE content LIKE ? OR tags LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (like, like, limit),
        )
        return cur.fetchall()

    def recent(self, limit: int = 10) -> list[tuple[int, str, str, str]]:
        cur = self.conn.execute(
            "SELECT id, created_at, content, tags FROM memories ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()

    def delete(self, memory_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def wipe(self) -> int:
        cur = self.conn.execute("DELETE FROM memories")
        self.conn.commit()
        return cur.rowcount

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def memory_block(self, limit: int = 12) -> str:
        """注入系统提示词的记忆块。"""
        rows = self.recent(limit)
        if not rows:
            return "（暂无长期记忆。当用户透露值得记住的信息时，用 remember 工具保存。）"
        lines = [f"- 「#{mid} {created[:10]}」{content}" + (f"  [标签: {tags}]" if tags else "") for mid, created, content, tags in rows]
        return "\n".join(lines)


def _format_rows(rows: list[tuple[int, str, str, str]]) -> str:
    if not rows:
        return "（没有找到相关记忆）"
    lines = []
    for mid, created, content, tags in rows:
        lines.append(f"#{mid}  {created[:16]}  {content}" + (f"  [标签: {tags}]" if tags else ""))
    return "\n".join(lines)


def build_memory_tools(memory: LongTermMemory) -> list:
    """构建绑定了 memory 实例的三个工具。"""
    from langchain_core.tools import tool

    @tool
    def remember(content: str, tags: str = "") -> str:
        """将关于用户的长期信息存入记忆：身份背景、偏好、长期项目、重要决定等。
        不要存一时性的琐事。tags 为可选的逗号分隔标签，如 "偏好,饮食"。"""
        mid = memory.add(content, tags)
        return f"已记住（记忆编号 #{mid}）：{content}"

    @tool
    def recall(query: str = "") -> str:
        """检索长期记忆。query 为关键词（在内容和标签里做模糊匹配）；
        留空则返回最近的若干条记忆。"""
        rows = memory.search(query) if query.strip() else memory.recent(8)
        return _format_rows(rows)

    @tool
    def forget(memory_id: int) -> str:
        """按编号删除一条长期记忆。编号可从 recall 的结果中获取。"""
        if memory.delete(memory_id):
            return f"已删除记忆 #{memory_id}"
        return f"没有找到编号为 #{memory_id} 的记忆"

    return [remember, recall, forget]
