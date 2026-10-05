"""CLI/运行时架构回归：快照隔离、中断恢复和初始化失败清理。"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from rich.console import Console

import nuvora.cli as cli
from nuvora.config import Config, ConfigStore
from nuvora.runtime import AgentRuntime
from nuvora.sessions import SessionRepository
from tests.fakes import OfflineModel


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nuvora-architecture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = Config()
        self.cfg.model.model = "offline-model"
        self.cfg.model.api_key = "private-fixture"
        self.replies = [AIMessage(content="离线回复")]
        env = patch.dict(os.environ, {"NUVORA_API_KEY": "", "NUVORA_BASE_URL": "", "NUVORA_MODEL": ""})
        env.start()
        self.addCleanup(env.stop)

    def session(self):
        with patch.object(cli, "DATA_DIR", self.root / "data"), \
             patch.object(cli, "WORKSPACE_DIR", self.root / "workspace"), \
             patch.object(cli, "CONFIG_PATH", self.root / "config.toml"):
            session = cli.ChatSession(self.cfg)
        self.addCleanup(session.close)
        return session

    def runtime(self, session, replies):
        return AgentRuntime(session.cfg, OfflineModel(responses=replies), session.memory, session.checkpointer,
                            workspace=session.workspace, thread_id=session.thread_id)

    def run_turn(self, session, replies, text="你好"):
        with closing(self.runtime(session, replies).stream(text)) as events:
            return list(events)

    def test_configuration_snapshots_and_drafts_do_not_mutate_saved_state(self):
        store = ConfigStore(self.root / "config.toml", self.root / "data", self.cfg)
        self.cfg.model.model = "external-change"
        snapshot = store.snapshot()
        snapshot.model.api_key = "changed-key"
        draft = store.candidate({"model": {"model": "draft-model"}})
        draft.tools.web_enabled = False
        self.assertEqual(store.snapshot().model.model, "offline-model")
        self.assertEqual(store.snapshot().model.api_key, "private-fixture")
        self.assertTrue(store.snapshot().tools.web_enabled)
        self.assertFalse(store.path.exists())
        self.assertNotIn("private-fixture", str(store.public_state()))

    def test_failed_config_write_preserves_file_and_runtime_snapshot(self):
        store = ConfigStore(self.root / "config.toml", self.root / "data", self.cfg)
        store.update({})
        before = store.path.read_bytes()
        with patch("nuvora.config.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                store.update({"model": {"model": "unsaved-model"}})
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(store.snapshot().model.model, "offline-model")

    def test_abandoned_tool_stream_is_recoverable_without_manual_finish(self):
        session = self.session()
        replies = [AIMessage(content="", tool_calls=[{"name": "list_dir", "args": {"path": "."},
                                                      "id": "abandoned-call", "type": "tool_call"}]),
                   AIMessage(content="正常完成")]
        runtime = self.runtime(session, replies)
        with closing(runtime.stream("提前断开")) as events:
            for event in events:
                if event.kind == "message" and isinstance(event.value, AIMessage) and event.value.tool_calls:
                    break
        rows = session.repository.messages(session.thread_id)
        calls = {call["id"] for row in rows if isinstance(row, AIMessage) for call in row.tool_calls}
        results = {row.tool_call_id for row in rows if isinstance(row, ToolMessage)}
        self.assertEqual(calls, {"abandoned-call"})
        self.assertEqual(calls, results)
        result = self.run_turn(session, [AIMessage(content="断开后继续正常")])
        self.assertEqual(result[-1].value.content, "断开后继续正常")

    def test_past_tools_do_not_change_recovery_node_of_latest_plain_response(self):
        session = self.session()
        self.run_turn(session, [
            AIMessage(content="", tool_calls=[{"name": "list_dir", "args": {"path": "."},
                                               "id": "old-call", "type": "tool_call"}]),
            AIMessage(content="上一回合工具完成"),
        ])
        runtime = self.runtime(session, [AIMessage(content="当前普通回复")])
        cancel = Event()
        with closing(runtime.stream("停止普通回复", tokens=True, cancel=cancel)) as events:
            for event in events:
                if event.kind == "token":
                    cancel.set()
        self.assertEqual(runtime.graph.get_state(runtime.config).next, ())
        self.assertEqual(session.repository.messages(session.thread_id)[-1].content, "当前普通回复")
        self.assertEqual(self.run_turn(session, [AIMessage(content="继续正常")])[-1].value.content, "继续正常")

    def test_terminal_interrupt_closes_tool_stream_and_allows_next_turn(self):
        session = self.session()
        model = OfflineModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "list_dir", "args": {"path": "."},
                                               "id": "terminal-call", "type": "tool_call"}]),
            AIMessage(content="不应继续"),
        ])
        output = io.StringIO()
        with patch.object(cli, "console", Console(file=output)), \
             patch.object(session, "_build_model", return_value=model), \
             patch("nuvora.cli.TerminalOutput.event", side_effect=KeyboardInterrupt()):
            session.chat_turn("终端中断")
        self.assertIn("已打断", output.getvalue())
        self.assertTrue(any(isinstance(row, ToolMessage) and row.tool_call_id == "terminal-call"
                            for row in session.repository.messages(session.thread_id)))
        with patch.object(cli, "console", Console(file=output)), \
             patch.object(session, "_build_model", return_value=OfflineModel(responses=[AIMessage(content="终端继续正常")])):
            session.chat_turn("继续")
        self.assertIn("终端继续正常", output.getvalue())

    def test_stream_close_failure_still_reconciles_pending_message(self):
        session = self.session()
        runtime = self.runtime(session, [])
        class BrokenStream:
            def __iter__(self):
                return self
            def __next__(self):
                return "messages", (AIMessageChunk(content="开始输出"), {})
            def close(self):
                raise RuntimeError("close failed")
        graph = Mock()
        graph.stream.return_value = BrokenStream()
        graph.get_state.return_value = SimpleNamespace(values={"messages": [
            HumanMessage(content="输入"), AIMessage(content="部分回复"),
        ]})
        runtime.graph = graph
        stream = runtime.stream("输入", tokens=True)
        self.assertEqual(next(stream).kind, "token")
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            stream.close()
        self.assertEqual(graph.update_state.call_args.kwargs["as_node"], "model")

    def test_cancelled_unstarted_turn_does_not_write_history(self):
        session = self.session()
        cancel = Event()
        cancel.set()
        self.assertEqual(list(self.runtime(session, []).stream("未开始", cancel=cancel)), [])
        self.assertIsNone(session.checkpointer.get_tuple({"configurable": {"thread_id": session.thread_id}}))

    def test_memory_startup_failure_closes_session_databases(self):
        repositories = []
        def repository(path):
            value = SessionRepository(path)
            repositories.append(value)
            return value
        with patch.object(cli, "DATA_DIR", self.root / "data"), \
             patch.object(cli, "WORKSPACE_DIR", self.root / "workspace"), \
             patch.object(cli, "SessionRepository", side_effect=repository), \
             patch.object(cli, "LongTermMemory", side_effect=RuntimeError("memory failed")):
            with self.assertRaisesRegex(RuntimeError, "memory failed"):
                cli.ChatSession(self.cfg)
        with self.assertRaises(sqlite3.ProgrammingError):
            repositories[0].checkpointer.conn.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            repositories[0].list()


if __name__ == "__main__":
    unittest.main()
