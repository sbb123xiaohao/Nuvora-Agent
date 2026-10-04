"""本地 HTTP、真实 Agent 流、配置保存和旧会话恢复的端到端回归。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage

from nuvora.agent import build_agent, build_system_prompt
from nuvora.config import Config, load_config
from nuvora.llm import build_chat_model
from nuvora.tools import build_tools
from nuvora.web_ui import WebApplication, WebHandler, WebServer
from tests.fakes import OfflineModel


def events(response):
    result = []
    for frame in response.text.strip().split("\n\n"):
        if not frame:
            continue
        lines = frame.splitlines()
        name = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        result.append((name, data))
    return result


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nuvora-ui-test-")
        self.root = Path(self.tmp.name)
        self.cfg = Config()
        self.cfg.model.model = "offline-model"
        self.cfg.model.api_key = "regression-private-key"
        self.factory_calls = []
        self.replies = [AIMessage(content="离线模型的真实 Agent 回复")]
        self.env = patch.dict(os.environ, {"NUVORA_API_KEY": "", "NUVORA_BASE_URL": "", "NUVORA_MODEL": "",
                                          "LANGSMITH_TRACING": "false", "LANGSMITH_TRACING_V2": "false"})
        self.env.start()
        self.start(self.cfg)

    def model(self, cfg):
        self.factory_calls.append(cfg)
        return OfflineModel(responses=self.replies)

    def start(self, cfg=None):
        with patch("nuvora.application.sandbox_status", return_value=(False, "测试环境不执行 Python")):
            self.app = WebApplication(cfg, data_dir=self.root / "data", workspace=self.root / "workspace",
                                      config_path=self.root / "config.toml", model_factory=self.model)
        self.server = WebServer(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = httpx.Client(base_url=self.url, timeout=10, trust_env=False)
        self.client.get("/").raise_for_status()
        state = self.client.get("/api/state").json()
        self.client.headers["X-Nuvora-CSRF"] = state["csrf_token"]
        self.tid = state["active_thread"]

    def shutdown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()

    def tearDown(self):
        self.shutdown()
        self.env.stop()
        self.tmp.cleanup()

    def post(self, path, payload=None):
        return self.client.post(path, json=payload or {})

    def chat(self, text="你好"):
        response = self.post("/api/chat", {"thread_id": self.tid, "text": text})
        self.assertEqual(response.status_code, 200, response.text)
        return events(response)

    def test_private_state_and_origin_cookie_csrf_guards(self):
        response = self.client.get("/api/state")
        self.assertNotIn(self.cfg.model.api_key, response.text)
        self.assertTrue(response.json()["config"]["model"]["api_key_configured"])
        self.assertNotIn("api_key", response.json()["config"]["model"])
        with httpx.Client(base_url=self.url, trust_env=False) as anonymous:
            self.assertEqual(anonymous.get("/api/state").status_code, 403)
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example"},
                        {"X-Nuvora-CSRF": "wrong"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.client.post("/api/config", json={}, headers=headers).status_code, 403)
        self.assertFalse((self.root / "config.toml").exists())

    def test_assets_are_local_and_have_security_headers(self):
        for path in ("/", "/style.css", "/app.js"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertIn("HttpOnly", self.client.get("/").headers["set-cookie"])
        self.assertEqual(self.client.get("/../config.toml").status_code, 404)

    def test_save_restore_blank_key_and_explicit_clear(self):
        result = self.post("/api/config", {"model": {"model": "saved-model", "api_key": "", "temperature": .4},
                                          "tools": {"web_enabled": False}, "memory": {"enabled": False}})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn(self.cfg.model.api_key, result.text)
        saved = load_config(self.root / "config.toml")
        self.assertEqual(saved.model.api_key, self.cfg.model.api_key)
        self.assertEqual(saved.model.model, "saved-model")
        self.assertFalse(saved.tools.web_enabled)
        if os.name == "posix":
            self.assertEqual((self.root / "config.toml").stat().st_mode & 0o777, 0o600)
        self.shutdown()
        self.start()
        state = self.client.get("/api/state").json()
        self.assertEqual(state["config"]["model"]["model"], "saved-model")
        self.assertFalse(state["config"]["memory"]["enabled"])
        self.assertEqual(self.post("/api/config", {"clear_api_key": True}).status_code, 200)
        self.assertEqual(load_config(self.root / "config.toml").model.api_key, "")

    def test_invalid_settings_do_not_replace_valid_file(self):
        self.post("/api/config")
        before = (self.root / "config.toml").read_bytes()
        for payload in ({"tools": {"web_enabled": "false"}}, {"model": {"temperature": 3}},
                        {"agent": {"max_iterations": 0}}, {"model": {"api_key": 9}},
                        {"model": {"base_url": "file:///etc/passwd"}}):
            self.assertEqual(self.post("/api/config", payload).status_code, 400)
            self.assertEqual((self.root / "config.toml").read_bytes(), before)

    def test_corrupt_config_can_be_repaired_in_interface(self):
        self.shutdown()
        original = b'[model]\napi_key = "sensitive-invalid\n'
        (self.root / "config.toml").write_bytes(original)
        self.start()
        state = self.client.get("/api/state")
        self.assertIn("重新填写", state.json()["config_warning"])
        self.assertNotIn("sensitive-invalid", state.text)
        self.assertEqual((self.root / "config.toml").read_bytes(), original)
        self.assertEqual(self.post("/api/config", {"model": {"model": "repaired"}}).status_code, 200)
        backups = list((self.root / "data").glob("config-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(load_config(self.root / "config.toml").model.model, "repaired")

    def test_environment_override_does_not_pretend_to_save(self):
        with patch.dict(os.environ, {"NUVORA_MODEL": "environment-model"}):
            response = self.post("/api/config", {"model": {"model": "another-model"}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("NUVORA_MODEL", response.json()["error"])

    def test_discovery_and_connection_use_unsaved_draft_without_leaking_key(self):
        captured = []
        def discover(cfg):
            captured.append(cfg)
            return False, [], "failed with " + cfg.model.api_key
        with patch("nuvora.application.list_available_models", side_effect=discover):
            response = self.post("/api/models", {"model": {"api_key": "draft-private", "base_url": "http://localhost:9000/v1"}})
        self.assertEqual(captured[0].model.base_url, "http://localhost:9000/v1")
        self.assertNotIn("draft-private", response.text)
        self.assertFalse((self.root / "config.toml").exists())
        with patch("nuvora.application.ping_model", return_value=(True, "连接正常")):
            self.assertTrue(self.post("/api/test").json()["ok"])

    def test_real_streamed_tool_cycle_and_restart_history(self):
        self.replies = [
            AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"path": "notes/result.txt", "content": "UI 工作区内容"},
                                              "id": "ui-tool", "type": "tool_call"}]),
            AIMessage(content="文件已经保存。"),
        ]
        emitted = self.chat("帮我保存一个文件")
        self.assertIn("tool_start", [name for name, _ in emitted])
        self.assertIn("tool_result", [name for name, _ in emitted])
        self.assertEqual(emitted[-1][0], "done")
        self.assertEqual((self.root / "workspace/notes/result.txt").read_text(), "UI 工作区内容")
        old_tid = self.tid
        self.shutdown()
        self.start(self.cfg)
        history = self.client.get("/api/session", params={"id": old_tid}).json()["messages"]
        self.assertEqual(history[-1]["text"], "文件已经保存。")
        self.assertIn(old_tid, [item["id"] for item in self.client.get("/api/state").json()["sessions"]])

    def test_live_config_reaches_next_real_agent_turn_and_stream_switch(self):
        result = self.post("/api/config", {"model": {"model": "changed-model", "temperature": .2}, "cli": {"stream": False}})
        self.assertEqual(result.status_code, 200)
        emitted = self.chat()
        self.assertNotIn("token", [name for name, _ in emitted])
        self.assertEqual(emitted[-1][1]["messages"][-1]["text"], "离线模型的真实 Agent 回复")
        self.assertEqual(self.factory_calls[-1].model.model, "changed-model")
        self.assertEqual(self.factory_calls[-1].model.temperature, .2)

    def test_tool_and_memory_switches_remove_capabilities_and_prompt_data(self):
        self.app.memory.add("private memory fixture")
        self.post("/api/config", {"tools": {"web_enabled": False, "files_enabled": False, "python_enabled": False},
                                  "memory": {"enabled": False}})
        self.assertEqual({tool.name for tool in build_tools(self.app.cfg, self.app.memory)}, {"current_time", "system_info"})
        self.assertNotIn("private memory fixture", build_system_prompt(self.app.cfg, self.app.memory))
        self.assertEqual(self.app.memory.count(), 1)

    def test_busy_turn_rejects_mutation_and_accepts_stop(self):
        with self.app.begin_turn({"text": "busy", "thread_id": self.tid}):
            for path in ("/api/config", "/api/session", "/api/chat"):
                payload = {"text": "other"} if path == "/api/chat" else {}
                self.assertEqual(self.post(path, payload).status_code, 409)
            self.assertTrue(self.post("/api/stop").json()["requested"])
        self.assertFalse(self.client.get("/api/state").json()["busy"])

    def test_stop_leaves_tool_messages_recoverable(self):
        self.replies = [AIMessage(content="", tool_calls=[{"name": "list_dir", "args": {"path": "."},
                                                          "id": "stop-tool", "type": "tool_call"}]),
                        AIMessage(content="工具之后的回复")]
        emitted = []
        with self.app.begin_turn({"text": "停止测试", "thread_id": self.tid}) as turn:
            for name, data in turn.events():
                emitted.append(name)
                if name == "tool_start":
                    self.app.stop()
        self.assertIn("stopped", emitted)
        history = self.app.history(self.tid)
        calls = {c["id"] for m in history for c in m.get("calls", [])}
        results = {m["call_id"] for m in history if m["role"] == "tool"}
        self.assertTrue(calls <= results)
        self.replies = [AIMessage(content="继续对话正常")]
        self.assertEqual(self.chat("继续")[-1][0], "done")

    def test_memory_and_workspace_actions_and_path_boundary(self):
        result = self.post("/api/memory", {"action": "add", "content": "喜欢蓝色", "tags": "偏好"})
        mid = result.json()["id"]
        rows = self.client.get("/api/memory", params={"q": "蓝色"}).json()["items"]
        self.assertEqual(rows[0]["id"], mid)
        self.assertTrue(self.post("/api/memory", {"action": "delete", "id": mid}).json()["deleted"])
        self.assertEqual(self.post("/api/file", {"path": "notes/test.md", "text": "# 测试"}).status_code, 200)
        self.assertEqual(self.client.get("/api/file", params={"path": "notes/test.md"}).json()["text"], "# 测试")
        self.assertIn("notes/", self.client.get("/api/files").json()["text"])
        self.assertEqual(self.client.get("/api/file", params={"path": "../config.toml"}).status_code, 400)
        self.assertEqual(self.post("/api/file", {"path": "../outside.txt", "text": "bad"}).status_code, 400)
        self.assertFalse((self.root / "outside.txt").exists())
        (self.root / "workspace/large.txt").write_text("x" * 21000)
        self.assertTrue(self.client.get("/api/file", params={"path": "large.txt"}).json()["truncated"])

    def test_legacy_cli_checkpoints_are_available_in_interface(self):
        graph = build_agent(self.cfg, OfflineModel(responses=[AIMessage(content="旧对话")]), self.app.memory,
                            self.app.checkpointer, workspace=self.app.workspace)
        graph.invoke({"messages": [HumanMessage(content="旧终端消息")]}, {"configurable": {"thread_id": "legacy-cli"}})
        state = self.client.get("/api/state").json()
        self.assertIn("legacy-cli", [session["id"] for session in state["sessions"]])
        self.assertEqual(self.client.get("/api/session", params={"id": "legacy-cli"}).json()["messages"][0]["text"], "旧终端消息")

    def test_failed_model_turn_returns_sanitized_error_and_releases_busy_state(self):
        def failure(cfg):
            raise RuntimeError("bad credential " + cfg.model.api_key)
        self.app.model_factory = failure
        result = self.chat()
        self.assertEqual(result[-1][0], "error")
        self.assertNotIn(self.cfg.model.api_key, json.dumps(result))
        self.assertFalse(self.client.get("/api/state").json()["busy"])

    def test_disconnect_before_stream_headers_releases_reserved_turn(self):
        headers = WebHandler._headers
        def disconnect(handler, status, mime, **kwargs):
            if mime.startswith("text/event-stream"):
                raise ConnectionResetError("client disconnected")
            return headers(handler, status, mime, **kwargs)
        with patch.object(WebHandler, "_headers", disconnect):
            with self.assertRaises(httpx.RemoteProtocolError):
                self.post("/api/chat", {"thread_id": self.tid, "text": "断开连接"})
        self.assertFalse(self.client.get("/api/state").json()["busy"])
        self.assertEqual(self.chat("重新发送")[-1][0], "done")

    def test_real_compatible_http_endpoint_discovery_stream_stop_and_continue(self):
        requests = []
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                raw = json.dumps({"data": [{"id": "compatible-test-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.headers.get("Authorization"), body))
                if not body.get("stream"):
                    data = {"id": "compatible", "object": "chat.completion", "created": 1, "model": body["model"],
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": "正常"}, "finish_reason": "stop"}]}
                    raw = json.dumps(data).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for text in ("真实", "兼容", "接口", "流式", "回复"):
                        chunk = {"id": "compatible-stream", "object": "chat.completion.chunk", "created": 1,
                                 "model": body["model"], "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                        self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
                        self.wfile.flush()
                        time.sleep(.025)
                    chunk["choices"][0] = {"index": 0, "delta": {}, "finish_reason": "stop"}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        thread = threading.Thread(target=endpoint.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            self.app.model_factory = build_chat_model
            draft = {"model": {"base_url": f"http://127.0.0.1:{endpoint.server_port}/v1",
                               "api_key": "fixture-only-key", "model": "compatible-test-model"}}
            self.assertTrue(self.post("/api/models", draft).json()["ok"])
            self.assertTrue(self.post("/api/test", draft).json()["ok"])
            self.assertEqual(self.post("/api/config", draft).status_code, 200)
            names = []
            with self.client.stream("POST", "/api/chat", json={"thread_id": self.tid, "text": "HTTP 兼容流测试"}) as response:
                self.assertEqual(response.status_code, 200)
                stopped = False
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        name = line[7:]
                        names.append(name)
                        if name == "token" and not stopped:
                            self.assertTrue(self.post("/api/stop").json()["requested"])
                            stopped = True
            self.assertIn("stopped", names)
            self.assertEqual(self.chat("停止后继续")[-1][0], "done")
            self.assertEqual(requests[-1][0], "Bearer fixture-only-key")
            self.assertEqual(requests[-1][1]["model"], "compatible-test-model")
            self.assertNotIn("fixture-only-key", self.client.get("/api/state").text)
        finally:
            endpoint.shutdown()
            endpoint.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
