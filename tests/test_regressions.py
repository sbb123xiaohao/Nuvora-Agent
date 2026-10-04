"""无 API 回归：真实 SQLite/Agent 循环、并发、会话恢复及执行边界。"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from rich.console import Console

import nuvora.cli as cli
import nuvora.config as config_module
import nuvora.tools as registry
from nuvora.agent import build_agent, list_thread_ids, open_checkpointer
from nuvora.config import Config, ConfigError, load_config
from nuvora.llm import list_available_models, run_doctor
from nuvora.memory import LongTermMemory, build_memory_tools
from nuvora.tools import files, python_repl, web
from nuvora.tools._python_sandbox import SandboxUnavailable, run_process, sandbox_command
from tests.fakes import OfflineModel


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nuvora-regression-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def memory(self):
        memory = LongTermMemory(self.root / "memory.db")
        self.addCleanup(memory.close)
        return memory

    def checkpointer(self):
        saver = open_checkpointer(self.root / "checkpoint.db")
        self.addCleanup(saver.conn.close)
        return saver

    def session(self):
        cfg = Config()
        cfg.model.model = "base-model"
        with patch.object(cli, "DATA_DIR", self.root), patch.object(cli, "WORKSPACE_DIR", self.workspace):
            session = cli.ChatSession(cfg)
        self.addCleanup(session.close)
        return session


class FileTests(WorkspaceCase):
    def test_default_root_and_normal_io(self):
        self.assertIn("已新建", files.sandbox_write_file(self.workspace, "notes/hello.txt", "hello"))
        self.assertEqual(files.sandbox_read_file(self.workspace, "notes/hello.txt"), "hello")
        self.assertIn("notes/", files.sandbox_list_dir(self.workspace))

    def test_escape_is_a_tool_result(self):
        for path in ("../outside", "a/../../outside", "/etc/passwd", "C:/Windows/win.ini", "", "a\0b"):
            with self.subTest(path=path):
                with self.assertRaises(files.SandboxViolation):
                    files.resolve_in_sandbox(path, self.workspace)
                self.assertIn("失败", files.sandbox_read_file(self.workspace, path))
                self.assertIn("失败", files.sandbox_write_file(self.workspace, path, "bad"))

    @unittest.skipUnless(os.name == "posix", "需要 POSIX 符号链接")
    def test_symlink_escape_is_blocked(self):
        private = self.root / "private.txt"
        private.write_text("private marker")
        (self.workspace / "link").symlink_to(private)
        self.assertIn("失败", files.sandbox_read_file(self.workspace, "link"))
        self.assertIn("失败", files.sandbox_write_file(self.workspace, "link", "changed"))
        self.assertEqual(private.read_text(), "private marker")

    @unittest.skipUnless(os.name == "posix", "需要 POSIX 目录句柄")
    def test_directory_swap_after_validation_is_blocked(self):
        safe = self.workspace / "safe"
        safe.mkdir()
        (safe / "note").write_text("safe")
        private = self.root / "private"
        private.mkdir()
        (private / "note").write_text("private marker")
        resolve = files.resolve_in_sandbox

        def swap(path, root):
            target = resolve(path, root)
            safe.rename(self.workspace / "old-safe")
            safe.symlink_to(private, target_is_directory=True)
            return target

        with patch.object(files, "resolve_in_sandbox", side_effect=swap):
            result = files.sandbox_read_file(self.workspace, "safe/note")
        self.assertIn("失败", result)
        self.assertNotIn("private marker", result)

    @unittest.skipUnless(os.name == "posix", "需要 POSIX 符号链接")
    def test_leaf_swap_does_not_overwrite_outside(self):
        private = self.root / "private.txt"
        private.write_text("private marker")
        target = self.workspace / "note"
        target.write_text("old")
        resolve = files.resolve_in_sandbox

        def swap(path, root):
            result = resolve(path, root)
            target.unlink()
            target.symlink_to(private)
            return result

        with patch.object(files, "resolve_in_sandbox", side_effect=swap):
            files.sandbox_write_file(self.workspace, "note", "new")
        self.assertEqual(private.read_text(), "private marker")
        self.assertEqual(target.read_text(), "new")
        self.assertFalse(target.is_symlink())

    def test_atomic_failure_preserves_existing_content(self):
        target = self.workspace / "note"
        target.write_text("old")
        with patch.object(files.os, "replace", side_effect=OSError("replace failed")):
            self.assertIn("失败", files.sandbox_write_file(self.workspace, "note", "new"))
        self.assertEqual(target.read_text(), "old")
        self.assertEqual([p.name for p in self.workspace.iterdir()], ["note"])

    def test_large_file_has_bounded_result(self):
        (self.workspace / "large").write_text("a" * 1_000_000)
        result = files.sandbox_read_file(self.workspace, "large")
        self.assertLess(len(result), 20100)
        self.assertIn("截断", result)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "需要 FIFO 支持")
    def test_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.workspace / "pipe")
        self.assertIn("失败", files.sandbox_read_file(self.workspace, "pipe"))


class MemoryTests(WorkspaceCase):
    def test_parallel_writes_are_all_committed_with_unique_ids(self):
        memory = self.memory()
        with ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(lambda n: memory.add(f"fact {n}"), range(480)))
        self.assertEqual(memory.count(), 480)
        self.assertEqual(len(set(ids)), 480)
        memory.close()
        reopened = LongTermMemory(memory.db_path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.count(), 480)

    def test_literal_search_and_delete(self):
        memory = self.memory()
        mid = memory.add("100% preference")
        memory.add("1000 preference")
        self.assertEqual([r[0] for r in memory.search("%")], [mid])
        self.assertTrue(memory.delete(mid))
        self.assertFalse(memory.search("%"))

    def test_memory_tool_errors_remain_observations(self):
        memory = self.memory()
        tools = build_memory_tools(memory)
        self.assertIn("失败", tools[0].invoke({"content": ""}))
        memory.close()
        self.assertIn("失败", tools[1].invoke({"query": "fact"}))
        self.assertIn("失败", tools[2].invoke({"memory_id": 1}))


class AgentTests(WorkspaceCase):
    def invoke(self, model, saver, memory=None, thread="test"):
        with patch.object(registry, "WORKSPACE_DIR", self.workspace):
            graph = build_agent(Config(), model, memory, saver)
            return graph.invoke({"messages": [("user", "first turn")]}, {"configurable": {"thread_id": thread}})

    def test_real_sqlite_tool_cycle_and_reopen(self):
        saver = self.checkpointer()
        model = OfflineModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"path": "note", "content": "marker"}, "id": "write-1", "type": "tool_call"}]),
            AIMessage(content="saved"),
        ])
        self.assertEqual(self.invoke(model, saver)["messages"][-1].content, "saved")
        self.assertEqual((self.workspace / "note").read_text(), "marker")
        saver.conn.close()
        reopened = self.checkpointer()
        followup = OfflineModel(responses=[AIMessage(content="continued")])
        with patch.object(registry, "WORKSPACE_DIR", self.workspace):
            graph = build_agent(Config(), followup, None, reopened)
            graph.invoke({"messages": [("user", "second turn")]}, {"configurable": {"thread_id": "test"}})
        self.assertTrue(any(m.content == "first turn" for m in followup.seen[0]))

    def test_root_listing_completes_agent(self):
        (self.workspace / "marker").write_text("marker")
        model = OfflineModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "list_dir", "args": {}, "id": "list-1", "type": "tool_call"}]),
            AIMessage(content="listed"),
        ])
        self.assertEqual(self.invoke(model, self.checkpointer())["messages"][-1].content, "listed")
        self.assertTrue(any(isinstance(m, ToolMessage) and "marker" in m.content for m in model.seen[1]))

    def test_path_error_does_not_abort_agent(self):
        model = OfflineModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "../private"}, "id": "read-1", "type": "tool_call"}]),
            AIMessage(content="recovered"),
        ])
        self.assertEqual(self.invoke(model, self.checkpointer())["messages"][-1].content, "recovered")
        self.assertTrue(any(isinstance(m, ToolMessage) and "失败" in m.content for m in model.seen[1]))

    def test_parallel_memory_tools_in_real_graph(self):
        memory = self.memory()
        model = OfflineModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "remember", "args": {"content": f"parallel fact {n}"}, "id": f"memory-{n}", "type": "tool_call"} for n in range(20)]),
            AIMessage(content="remembered"),
        ])
        self.assertEqual(self.invoke(model, self.checkpointer(), memory)["messages"][-1].content, "remembered")
        self.assertEqual(memory.count(), 20)


class SessionTests(WorkspaceCase):
    def test_older_thread_is_listed_and_resumed_after_600_checkpoints(self):
        session = self.session()
        old = {"configurable": {"thread_id": "older", "checkpoint_ns": ""}}
        session.checkpointer.put(old, empty_checkpoint(), {"source": "input", "step": 0}, {})
        for n in range(600):
            session.checkpointer.put({"configurable": {"thread_id": "busy", "checkpoint_ns": ""}}, empty_checkpoint(), {"source": "input", "step": n}, {})
        self.assertIn("older", list_thread_ids(session.checkpointer))
        with patch.object(cli, "console", Console(file=io.StringIO())):
            session._cmd_resume("older")
        self.assertEqual(session.thread_id, "older")

    def test_model_override_is_scoped_to_current_thread(self):
        session = self.session()
        with patch.object(cli, "console", Console(file=io.StringIO())):
            session._cmd_model("temporary-model")
            self.assertEqual(session.model_name, "temporary-model")
            session.handle_command("/new")
        self.assertEqual(session.model_name, "base-model")
        session.thread_id = "default"
        self.assertEqual(session.model_name, "temporary-model")

    def test_close_is_idempotent_and_releases_databases(self):
        session = self.session()
        session.close()
        session.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            session.memory.conn.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            session.checkpointer.conn.execute("SELECT 1")

    def test_nonstream_mode_still_executes_real_agent(self):
        session = self.session()
        session.cfg.cli.stream = False
        model = OfflineModel(responses=[AIMessage(content="nonstream response")])
        output = io.StringIO()
        with patch.object(session, "_build_model", return_value=model), patch.object(registry, "WORKSPACE_DIR", self.workspace), patch.object(cli, "console", Console(file=output, width=100)):
            session.chat_turn("nonstream input")
        self.assertIn("nonstream response", output.getvalue())
        messages = session.checkpointer.get_tuple({"configurable": {"thread_id": "default"}}).checkpoint["channel_values"]["messages"]
        self.assertEqual(messages[-1].content, "nonstream response")


class ConfigTests(WorkspaceCase):
    def load(self, content):
        path = self.root / "config.toml"
        path.write_text(content)
        with patch.object(config_module, "CONFIG_PATH", path), patch.dict(os.environ, {}, clear=True):
            return load_config()

    def test_boolean_false_remains_false(self):
        self.assertFalse(self.load("[memory]\nenabled = false\n").memory.enabled)

    def test_string_boolean_and_negative_values_are_rejected(self):
        for content in ('[memory]\nenabled = "false"', '[agent]\nmax_iterations = -1', '[tools]\npython_timeout = 0', '[model]\ntemperature = nan'):
            with self.subTest(content=content), self.assertRaises(ConfigError):
                self.load(content)

    def test_invalid_toml_error_does_not_echo_key(self):
        with self.assertRaises(ConfigError) as raised:
            self.load('[model]\napi_key = "dummy-private-key"\nmodel = "unterminated')
        self.assertNotIn("dummy-private-key", str(raised.exception))


class ModelAndWebTests(WorkspaceCase):
    def models(self, payload, status=200):
        cfg = Config()
        cfg.model.api_key = "dummy-private-key"
        response = httpx.Response(status, json=payload, request=httpx.Request("GET", "https://example.invalid/models"))
        with patch("nuvora.llm.httpx.get", return_value=response):
            return list_available_models(cfg)

    def test_models_standard_response(self):
        self.assertEqual(self.models({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]})[1], ["a", "b"])

    def test_models_bad_shapes_fail_without_exception(self):
        for payload in ({"data": 42}, {"data": {"fake": "model"}}, 42):
            with self.subTest(payload=payload):
                self.assertFalse(self.models(payload)[0])

    def test_endpoint_error_redacts_api_key(self):
        self.assertNotIn("dummy-private-key", self.models({"error": "dummy-private-key"}, 500)[2])

    def test_socks_proxy_client_can_be_constructed(self):
        with httpx.Client(proxy="socks5://127.0.0.1:9"):
            pass

    def test_doctor_preserves_existing_probe_filename(self):
        marker = self.workspace / ".doctor_probe"
        marker.write_text("existing user file")
        with patch("nuvora.llm.WORKSPACE_DIR", self.workspace), patch("nuvora.llm.list_available_models", return_value=(False, [], "offline")), patch("nuvora.tools.python_repl.sandbox_status", return_value=(False, "unavailable")):
            run_doctor(Config())
        self.assertEqual(marker.read_text(), "existing user file")

    def test_web_download_limit_returns_error(self):
        response = httpx.Response(200, content=b"a" * (2 * 1024 * 1024 + 1), request=httpx.Request("GET", "https://example.invalid"))
        with patch("httpx.stream", return_value=nullcontext(response)):
            self.assertIn("下载上限", web.do_web_fetch("https://example.invalid"))


@unittest.skipUnless(sys.platform == "linux", "执行隔离仅支持 Linux/WSL")
class ExecutionTests(WorkspaceCase):
    def test_environment_does_not_inherit_secrets(self):
        with patch.dict(os.environ, {"NUVORA_API_KEY": "dummy-private-key"}):
            rc, stdout, _, _ = run_process([sys.executable, "-I", "-c", "import os; print(os.environ.get('NUVORA_API_KEY', 'absent'))"], 5, self.workspace)
        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "absent")

    def test_output_is_bounded_while_both_pipes_are_drained(self):
        rc, stdout, stderr, timed_out = run_process([sys.executable, "-I", "-c", "import sys; print('a'*1000000); print('b'*1000000, file=sys.stderr)"], 5, self.workspace)
        self.assertEqual(rc, 0)
        self.assertFalse(timed_out)
        self.assertLess(len(stdout), 4100)
        self.assertLess(len(stderr), 4100)
        self.assertIn("截断", stdout)

    def test_timeout_kills_descendants(self):
        child = "import time; from pathlib import Path; time.sleep(2); Path('survived').write_text('bad')"
        code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-I','-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(5)"
        _, _, _, timed_out = run_process([sys.executable, "-I", "-c", code], 1, self.workspace)
        self.assertTrue(timed_out)
        time.sleep(1.3)
        self.assertFalse((self.workspace / "survived").exists())

    def test_missing_sandbox_never_falls_back(self):
        with patch("nuvora.tools.python_repl.seccomp_policy", side_effect=SandboxUnavailable("missing sandbox")), patch("nuvora.tools.python_repl.run_process") as execute:
            result = python_repl.run_python_code("print('must not execute')", 5, self.workspace)
        self.assertIn("不可用", result)
        execute.assert_not_called()

    def test_missing_bwrap_is_rejected(self):
        with patch("nuvora.tools._python_sandbox.shutil.which", return_value=None):
            with self.assertRaises(SandboxUnavailable):
                sandbox_command(self.workspace, "print(42)", 5, 3)

    def test_actual_sandbox_boundaries_when_available(self):
        available, detail = python_repl.sandbox_status(self.workspace)
        if not available:
            self.skipTest(detail)
        private = self.root / "private-marker"
        private.write_text("PRIVATE_MARKER")
        code = (
            "import os,socket\nfrom pathlib import Path\n"
            f"try:\n Path({str(private)!r}).read_text()\n print('escaped')\nexcept OSError:\n print('outside blocked')\n"
            "try:\n socket.socket()\n print('network allowed')\nexcept OSError:\n print('network blocked')\n"
            "print('env clean' if 'NUVORA_API_KEY' not in os.environ else 'env leaked')\n"
            "Path('inside').write_text('ok')\nprint(6*7)"
        )
        with patch.dict(os.environ, {"NUVORA_API_KEY": "dummy-private-key"}):
            result = python_repl.run_python_code(code, 5, self.workspace)
        self.assertIn("outside blocked", result)
        self.assertIn("network blocked", result)
        self.assertIn("env clean", result)
        self.assertIn("42", result)
        self.assertEqual((self.workspace / "inside").read_text(), "ok")


if __name__ == "__main__":
    unittest.main()
