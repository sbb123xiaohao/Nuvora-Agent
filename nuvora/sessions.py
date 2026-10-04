"""会话仓库：检查点、标题与旧 CLI 会话，独立于界面和 HTTP。"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from .agent import list_thread_ids, open_checkpointer
from .errors import ApplicationError, NotFoundError


class SessionRepository:
    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 任一步初始化失败都会关闭已打开的连接。
        with ExitStack() as resources:
            self.checkpointer = open_checkpointer(data_dir / "checkpoints.db")
            resources.callback(self.checkpointer.conn.close)
            self._db = sqlite3.connect(data_dir / "interface.db", check_same_thread=False, timeout=10)
            resources.callback(self._db.close)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, updated TEXT NOT NULL)")
            self._db.commit()
            self._resources = resources.pop_all()

    @staticmethod
    def validate_id(thread_id: str) -> None:
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 200:
            raise ApplicationError("会话编号无效。")

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id,title,updated FROM sessions ORDER BY updated DESC").fetchall()
            result = [{"id": r[0], "title": r[1], "updated": r[2]} for r in rows]
            known = {r["id"] for r in result}
            for tid in list_thread_ids(self.checkpointer):
                if tid not in known:
                    result.append({"id": tid, "title": "历史会话 · " + tid[:8], "updated": ""})
            return result

    def touch(self, thread_id: str, title: str | None = None) -> None:
        self.validate_id(thread_id)
        with self._lock, self._db:
            now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            old = self._db.execute("SELECT title FROM sessions WHERE id=?", (thread_id,)).fetchone()
            label = title or (old[0] if old else "历史会话")
            self._db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, updated=excluded.updated",
                (thread_id, label[:80], now),
            )

    def new(self) -> dict:
        tid = uuid.uuid4().hex
        self.touch(tid, "新对话")
        return {"id": tid, "title": "新对话"}

    def record_turn(self, thread_id: str, text: str) -> None:
        with self._lock:
            old = self._db.execute("SELECT title FROM sessions WHERE id=?", (thread_id,)).fetchone()
            title = " ".join(text.split())[:40] if not old or old[0] == "新对话" else None
            self.touch(thread_id, title)

    def messages(self, thread_id: str) -> list:
        self.validate_id(thread_id)
        with self._lock:
            checkpoint = self.checkpointer.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
            if checkpoint is not None:
                return checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
            if not self._db.execute("SELECT 1 FROM sessions WHERE id=?", (thread_id,)).fetchone():
                raise NotFoundError("没有找到此会话。")
            return []

    def close(self) -> None:
        with self._lock:
            self._resources.close()
