"""架构边界回归：快照隔离、回合所有权、中断恢复和失败清理。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from nuvora.application import AgentApplication
from nuvora.config import Config, ConfigStore
from nuvora.errors import BusyError, ClosedError
from nuvora.memory import LongTermMemory
from nuvora.runtime import AgentRuntime, RuntimeEvent
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

    def application(self):
        with patch("nuvora.application.sandbox_status", return_value=(False, "不执行 Python")):
            app = AgentApplication(self.cfg, data_dir=self.root / "data", workspace=self.root / "workspace",
                                   config_path=self.root / "config.toml",
                                   model_factory=lambda _cfg: OfflineModel(responses=self.replies))
        self.addCleanup(app.close)
        return app

    def run_turn(self, app, text="你好"):
        with app.begin_turn({"text": text}) as turn:
            return list(turn.events())

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

    def test_failed_config_write_preserves_both_file_and_next_turn_config(self):
        app = self.application()
        app.update_config({})
        before = (self.root / "config.toml").read_bytes()
        with patch("nuvora.config.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                app.update_config({"model": {"model": "unsaved-model"}})
        self.assertEqual((self.root / "config.toml").read_bytes(), before)
        with app.begin_turn({"text": "仍用原配置"}) as turn:
            self.assertEqual(turn.cfg.model.model, "offline-model")

    def test_unstarted_and_stale_turns_cannot_keep_or_release_another_turn(self):
        app = self.application()
        with app.begin_turn({"text": "尚未执行"}) as old_turn:
            self.assertTrue(app.busy)
        self.assertFalse(app.busy)
        with app.begin_turn({"text": "新回合"}) as new_turn:
            old_turn.close()
            self.assertTrue(app.busy)
            with self.assertRaises(BusyError):
                app.begin_turn({"text": "第三个回合"})
            iterator = new_turn.events()
            with self.assertRaises(BusyError):
                new_turn.events()
            self.assertEqual(list(iterator)[-1][0], "done")

    def test_concurrent_begin_reserves_exactly_one_turn(self):
        app = self.application()
        barrier = threading.Barrier(8)
        def begin(_index):
            barrier.wait(timeout=3)
            try:
                return app.begin_turn({"text": "并发输入"})
            except BusyError:
                return None
        with ThreadPoolExecutor(max_workers=8) as executor:
            turns = [turn for turn in executor.map(begin, range(8)) if turn is not None]
        self.assertEqual(len(turns), 1)
        turns[0].close()
        self.assertFalse(app.busy)

    def test_stream_close_failure_still_releases_turn(self):
        app = self.application()
        class BrokenStream:
            def __iter__(self):
                return self
            def __next__(self):
                return RuntimeEvent("token", "开始输出")
            def close(self):
                raise RuntimeError("close failed")
        with patch("nuvora.application.AgentRuntime") as runtime:
            runtime.return_value.stream.return_value = BrokenStream()
            with self.assertRaisesRegex(RuntimeError, "close failed"):
                with app.begin_turn({"text": "中断"}) as turn:
                    iterator = turn.events()
                    self.assertEqual(next(iterator)[0], "start")
                    self.assertEqual(next(iterator)[0], "token")
        self.assertFalse(app.busy)
        self.assertEqual(self.run_turn(app)[-1][0], "done")

    def test_abandoned_tool_stream_is_recoverable_without_manual_finish(self):
        app = self.application()
        self.replies = [AIMessage(content="", tool_calls=[{"name": "list_dir", "args": {"path": "."},
                                                          "id": "abandoned-call", "type": "tool_call"}]),
                        AIMessage(content="正常完成")]
        with app.begin_turn({"text": "提前断开"}) as turn:
            for name, _data in turn.events():
                if name == "tool_start":
                    break
        rows = app.history(app.active_thread)
        calls = {call["id"] for row in rows for call in row.get("calls", [])}
        results = {row["call_id"] for row in rows if row["role"] == "tool"}
        self.assertEqual(calls, results)
        self.assertFalse(app.busy)
        self.replies = [AIMessage(content="断开后继续正常")]
        self.assertEqual(self.run_turn(app)[-1][1]["messages"][-1]["text"], "断开后继续正常")

    def test_past_tools_do_not_change_recovery_node_of_latest_plain_response(self):
        app = self.application()
        self.replies = [AIMessage(content="", tool_calls=[{"name": "list_dir", "args": {"path": "."},
                                                          "id": "old-call", "type": "tool_call"}]),
                        AIMessage(content="上一回合工具完成")]
        self.assertEqual(self.run_turn(app)[-1][0], "done")
        self.replies = [AIMessage(content="当前普通回复")]
        with app.begin_turn({"text": "停止普通回复"}) as turn:
            names = []
            for name, _data in turn.events():
                names.append(name)
                if name == "token":
                    app.stop()
        self.assertIn("stopped", names)
        runtime = AgentRuntime(app.cfg, OfflineModel(responses=[]), app.memory, app.checkpointer,
                               workspace=app.workspace, thread_id=app.active_thread)
        snapshot = runtime.graph.get_state(runtime.config)
        self.assertEqual(snapshot.next, ())
        self.assertEqual(app.history(app.active_thread)[-1]["text"], "当前普通回复")
        self.replies = [AIMessage(content="继续正常")]
        self.assertEqual(self.run_turn(app)[-1][0], "done")

    def test_application_shutdown_defers_database_close_until_turn_exit(self):
        app = self.application()
        turn = app.begin_turn({"text": "尚在运行"})
        app.close()
        self.assertTrue(turn.cancel.is_set())
        self.assertFalse(app.closed)
        self.assertEqual(app.checkpointer.conn.execute("SELECT 1").fetchone(), (1,))
        with self.assertRaises(ClosedError):
            app.new_session()
        turn.close()
        self.assertTrue(app.closed)
        for connection in (app.checkpointer.conn, app.memory.conn):
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        app.close()

    def test_startup_failure_closes_every_opened_database(self):
        repositories, memories = [], []
        def repository(path):
            value = SessionRepository(path)
            repositories.append(value)
            return value
        def memory(path):
            value = LongTermMemory(path)
            memories.append(value)
            return value
        with patch("nuvora.application.SessionRepository", side_effect=repository), \
             patch("nuvora.application.LongTermMemory", side_effect=memory), \
             patch("nuvora.application.sandbox_status", side_effect=RuntimeError("probe failed")):
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                AgentApplication(self.cfg, data_dir=self.root / "data", workspace=self.root / "workspace")
        for connection in (repositories[0].checkpointer.conn, memories[0].conn):
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            repositories[0].list()

    def test_slow_model_discovery_does_not_block_stop_or_state(self):
        app = self.application()
        started, release = threading.Event(), threading.Event()
        def discover(_cfg):
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test release timeout")
            return True, ["offline-model"], "正常"
        with app.begin_turn({"text": "占用回合"}), \
             patch("nuvora.application.list_available_models", side_effect=discover), \
             ThreadPoolExecutor(max_workers=3) as executor:
            probe = executor.submit(app.discover_models, {})
            try:
                self.assertTrue(started.wait(timeout=1))
                self.assertTrue(executor.submit(app.stop).result(timeout=1)["requested"])
                self.assertTrue(executor.submit(app.state).result(timeout=1)["busy"])
            finally:
                release.set()
            self.assertTrue(probe.result(timeout=1)["ok"])


if __name__ == "__main__":
    unittest.main()
