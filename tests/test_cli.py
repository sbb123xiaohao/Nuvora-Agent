"""终端配置：密钥、环境覆盖、原子保存和下一次真实 Agent 回合。"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from rich.console import Console

import nuvora.cli as cli
from nuvora.config import Config, ConfigStore, load_config
from nuvora.terminal_setup import configure
from tests.fakes import OfflineModel


class TerminalSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nuvora-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = Config()
        self.cfg.model.model = "original-model"
        self.cfg.model.api_key = "original-fixture-key"
        self.store = ConfigStore(self.root / "config.toml", self.root / "data", self.cfg)
        self.output = io.StringIO()
        self.console = Console(file=self.output, width=120, force_terminal=False)
        env = patch.dict(os.environ, {"NUVORA_API_KEY": "", "NUVORA_BASE_URL": "", "NUVORA_MODEL": ""})
        env.start()
        self.addCleanup(env.stop)

    def prompts(self, text=None, confirms=None):
        resources = ExitStack()
        resources.enter_context(patch("sys.stdin.isatty", return_value=True))
        resources.enter_context(patch("nuvora.terminal_setup.Prompt.ask", side_effect=text or ["http://127.0.0.1:9/v1", "", "terminal-model"]))
        resources.enter_context(patch("nuvora.terminal_setup.Confirm.ask", side_effect=confirms or [False, False, True]))
        return resources

    def test_save_model_and_hidden_key_without_echo(self):
        with self.prompts(text=["http://127.0.0.1:9/v1", "new-fixture-key", "terminal-model"]):
            cfg = configure(self.store, self.console)
        self.assertEqual(cfg.model.model, "terminal-model")
        self.assertEqual(load_config(self.store.path).model.api_key, "new-fixture-key")
        self.assertNotIn("new-fixture-key", self.output.getvalue())
        self.assertNotIn("original-fixture-key", self.output.getvalue())
        if os.name == "posix":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_blank_key_preserves_and_dash_explicitly_clears(self):
        with self.prompts():
            self.assertEqual(configure(self.store, self.console).model.api_key, "original-fixture-key")
        with self.prompts(text=["http://127.0.0.1:9/v1", "-", "terminal-model"]):
            self.assertEqual(configure(self.store, self.console).model.api_key, "")
        self.assertEqual(load_config(self.store.path).model.api_key, "")

    def test_cancel_does_not_write_configuration(self):
        with self.prompts(confirms=[False, False, False]):
            self.assertIsNone(configure(self.store, self.console))
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.store.snapshot().model.model, "original-model")

    def test_invalid_draft_fails_cleanly_without_replacing_configuration(self):
        self.store.update({})
        before = self.store.path.read_bytes()
        with self.prompts(text=["file:///outside", "invalid-draft-key"]):
            self.assertIsNone(configure(self.store, self.console))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertIn("配置未保存", self.output.getvalue())
        self.assertNotIn("invalid-draft-key", self.output.getvalue())

    def test_interrupted_input_does_not_save_partial_settings(self):
        with self.prompts(text=["http://127.0.0.1:9/v1", KeyboardInterrupt()]):
            self.assertIsNone(configure(self.store, self.console))
        self.assertFalse(self.store.path.exists())
        self.assertIn("已取消配置", self.output.getvalue())

    def test_noninteractive_setup_does_not_ask_for_password(self):
        with patch("sys.stdin.isatty", return_value=False), patch("nuvora.terminal_setup.Prompt.ask") as ask:
            self.assertIsNone(configure(self.store, self.console))
        ask.assert_not_called()
        self.assertFalse(self.store.path.exists())

    def test_environment_settings_are_preserved_without_secret_prompts(self):
        overrides = {"NUVORA_BASE_URL": "http://127.0.0.1:9/v1", "NUVORA_API_KEY": "env-fixture-key", "NUVORA_MODEL": "environment-model"}
        with patch.dict(os.environ, overrides), patch("sys.stdin.isatty", return_value=True), \
             patch("nuvora.terminal_setup.Prompt.ask") as ask, \
             patch("nuvora.terminal_setup.Confirm.ask", side_effect=[False, False, True]):
            store = ConfigStore(self.store.path, self.root / "data")
            cfg = configure(store, self.console)
        ask.assert_not_called()
        self.assertEqual(cfg.model.model, "environment-model")
        self.assertEqual(load_config(store.path).model.api_key, "env-fixture-key")
        self.assertNotIn("env-fixture-key", self.output.getvalue())

    def test_discovery_uses_unsaved_draft_and_redacts_endpoint_error(self):
        captured = []
        def discover(cfg):
            captured.append(cfg)
            return False, [], "endpoint failure with " + cfg.model.api_key
        with self.prompts(text=["http://127.0.0.1:9/v1", "discovery-fixture-key", "terminal-model"], confirms=[True, False, False]), \
             patch("nuvora.terminal_setup.list_available_models", side_effect=discover):
            self.assertIsNone(configure(self.store, self.console))
        self.assertEqual(captured[0].model.api_key, "discovery-fixture-key")
        self.assertFalse(self.store.path.exists())
        self.assertNotIn("discovery-fixture-key", self.output.getvalue())

    def test_failed_save_preserves_file_and_redacts_new_key(self):
        self.store.update({})
        before = self.store.path.read_bytes()
        with self.prompts(text=["http://127.0.0.1:9/v1", "failed-save-key", "terminal-model"]), \
             patch("nuvora.config.os.replace", side_effect=OSError("failed-save-key replace failed")):
            self.assertIsNone(configure(self.store, self.console))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot().model.api_key, "original-fixture-key")
        self.assertNotIn("failed-save-key", self.output.getvalue())

    def test_setup_changes_next_real_agent_turn_and_removes_disabled_tools(self):
        with patch.object(cli, "CONFIG_PATH", self.store.path), patch.object(cli, "DATA_DIR", self.root / "data"), \
             patch.object(cli, "WORKSPACE_DIR", self.root / "workspace"):
            session = cli.ChatSession(self.cfg)
        self.addCleanup(session.close)
        session.memory.add("disabled-memory-fixture")
        with patch.object(cli, "console", self.console):
            session.handle_command("/model temporary-model")
            with self.prompts(confirms=[False, True, False, False, False, False, False, True]), \
                 patch("nuvora.terminal_setup.IntPrompt.ask", side_effect=[15, 3, 6]), \
                 patch("nuvora.terminal_setup.FloatPrompt.ask", return_value=.3):
                session.handle_command("/setup")
            model = OfflineModel(responses=[AIMessage(content="terminal agent response")])
            captured = []
            def build(cfg, model_override=None):
                captured.append((cfg, model_override))
                return model
            with patch("nuvora.llm.build_chat_model", side_effect=build):
                session.chat_turn("使用已保存的配置")
        self.assertEqual(session.model_name, "terminal-model")
        self.assertEqual(captured[0][1], "terminal-model")
        self.assertFalse(captured[0][0].cli.stream)
        self.assertFalse(captured[0][0].memory.enabled)
        self.assertEqual(captured[0][0].model.temperature, .3)
        self.assertNotIn("disabled-memory-fixture", model.seen[0][0].content)
        self.assertNotIn("write_file", model.seen[0][0].content)
        self.assertIn("terminal agent response", self.output.getvalue())
        self.assertEqual(session.memory.count(), 1)


class TerminalCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nuvora-commands-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = Config()
        self.cfg.model.model = "offline-model"
        self.cfg.model.api_key = "command-fixture-secret"
        self.output = io.StringIO()
        self.console = Console(file=self.output, width=160)
        console_patch = patch.object(cli, "console", self.console)
        console_patch.start()
        self.addCleanup(console_patch.stop)

    def session(self):
        with patch.object(cli, "DATA_DIR", self.root / "data"), \
             patch.object(cli, "WORKSPACE_DIR", self.root / "workspace"), \
             patch.object(cli, "CONFIG_PATH", self.root / "config.toml"):
            session = cli.ChatSession(self.cfg)
        self.addCleanup(session.close)
        return session

    def test_empty_new_sessions_are_listed_and_can_be_resumed_after_restart(self):
        session = self.session()
        session.handle_command("/new")
        empty_id = session.thread_id
        session.handle_command("/new")
        session.handle_command("/resume " + empty_id)
        self.assertEqual(session.thread_id, empty_id)
        session.handle_command("/history")
        self.assertIn("还没有消息", self.output.getvalue())
        session.close()
        restored = self.session()
        restored.handle_command("/sessions")
        self.assertIn(empty_id, self.output.getvalue())
        restored.handle_command("/resume " + empty_id)
        self.assertEqual(restored.thread_id, empty_id)
        self.assertEqual(restored.repository.messages(empty_id), [])

    def test_titles_history_and_model_context_survive_restart(self):
        session = self.session()
        with patch.object(session, "_build_model", return_value=OfflineModel(responses=[AIMessage(content="saved reply")])):
            session.chat_turn("first topic [red]")
        session.close()
        restored = self.session()
        restored.handle_command("/sessions")
        restored.handle_command("/resume default")
        restored.handle_command("/history")
        self.assertIn("first topic [red]", self.output.getvalue())
        self.assertIn("saved reply", self.output.getvalue())
        model = OfflineModel(responses=[AIMessage(content="continued reply")])
        with patch.object(restored, "_build_model", return_value=model):
            restored.chat_turn("continue topic")
        self.assertIn("first topic [red]", [message.content for message in model.seen[0]])
        self.assertIn("saved reply", [message.content for message in model.seen[0]])

    def test_unknown_session_does_not_replace_active_session(self):
        session = self.session()
        session.handle_command("/new")
        active = session.thread_id
        session.handle_command("/resume nonexistent")
        self.assertEqual(session.thread_id, active)
        self.assertIn("没有找到会话", self.output.getvalue())

    def test_file_commands_share_workspace_boundary_and_keep_literal_content(self):
        session = self.session()
        session.cfg.tools.files_enabled = False
        (session.workspace / "file with spaces.txt").write_text("literal [red] content", encoding="utf-8")
        (self.root / "outside.txt").write_text("outside-secret", encoding="utf-8")
        session.handle_command("/files")
        session.handle_command("/read file with spaces.txt")
        session.handle_command("/read ../outside.txt")
        self.assertIn("file with spaces.txt", self.output.getvalue())
        self.assertIn("literal [red] content", self.output.getvalue())
        self.assertIn("文件操作失败", self.output.getvalue())
        self.assertNotIn("outside-secret", self.output.getvalue())

    def test_memory_commands_save_search_delete_and_respect_disabled_state(self):
        session = self.session()
        session.handle_command("/remember memory [red] fact")
        session.handle_command("/memory fact")
        self.assertEqual(session.memory.count(), 1)
        self.assertIn("memory [red] fact", self.output.getvalue())
        session.handle_command("/forget #1")
        self.assertEqual(session.memory.count(), 0)
        session.memory.add("preserved while disabled")
        session.cfg.memory.enabled = False
        session.handle_command("/remember ignored")
        session.handle_command("/forget 2")
        self.assertEqual(session.memory.count(), 1)

    def test_status_never_prints_api_key(self):
        session = self.session()
        session.handle_command("/model local-override")
        session.handle_command("/status")
        self.assertIn("local-override", self.output.getvalue())
        self.assertIn("已设置", self.output.getvalue())
        self.assertNotIn(self.cfg.model.api_key, self.output.getvalue())

    def test_removed_web_command_and_invalid_arguments_return_usage_errors(self):
        with patch.object(cli, "load_config") as load:
            self.assertEqual(cli.main(["web"]), 2)
            self.assertEqual(cli.main(["doctor", "--invalid"]), 2)
            self.assertEqual(cli.main(["models", "unexpected"]), 2)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
