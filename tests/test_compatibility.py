"""实际模型适配器与 CLI：本地 Chat Completions 端点，不调用云端。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from rich.console import Console

import nuvora.cli as cli
from nuvora.config import Config
from nuvora.llm import build_chat_model, list_available_models, ping_model
from nuvora.runtime import AgentRuntime

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGSMITH_TRACING_V2"] = "false"


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nuvora-compatible-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requests = []
        requests = self.requests

        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def json_response(self, data):
                raw = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization"), None))
                self.json_response({"data": [{"id": "compatible-test-model"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers.get("Authorization"), body))
                if not body.get("stream"):
                    self.json_response({"id": "compatible", "object": "chat.completion", "created": 1,
                                        "model": body["model"], "choices": [{"index": 0,
                                        "message": {"role": "assistant", "content": "nonstream reply"},
                                        "finish_reason": "stop"}]})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for text in ("compatible", " streaming", " reply"):
                        chunk = {"id": "compatible-stream", "object": "chat.completion.chunk", "created": 1,
                                 "model": body["model"], "choices": [{"index": 0,
                                 "delta": {"content": text}, "finish_reason": None}]}
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                        self.wfile.flush()
                    chunk["choices"][0] = {"index": 0, "delta": {}, "finish_reason": "stop"}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        self.thread = threading.Thread(target=self.endpoint.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_endpoint)
        self.cfg = Config()
        self.cfg.model.base_url = f"http://127.0.0.1:{self.endpoint.server_port}/v1"
        self.cfg.model.api_key = "fixture-only-key"
        self.cfg.model.model = "compatible-test-model"
        self.cfg.tools.web_enabled = self.cfg.tools.files_enabled = self.cfg.tools.python_enabled = False
        self.cfg.memory.enabled = False
        with patch.object(cli, "DATA_DIR", self.root / "data"), \
             patch.object(cli, "WORKSPACE_DIR", self.root / "workspace"), \
             patch.object(cli, "CONFIG_PATH", self.root / "config.toml"):
            self.session = cli.ChatSession(self.cfg)
        self.addCleanup(self.session.close)

    def stop_endpoint(self):
        self.endpoint.shutdown()
        self.endpoint.server_close()
        self.thread.join(timeout=2)

    def runtime(self):
        session = self.session
        return AgentRuntime(session.cfg, build_chat_model(session.cfg), session.memory, session.checkpointer,
                            workspace=session.workspace, thread_id=session.thread_id)

    def test_discovery_stream_cancellation_and_continue_with_real_adapter(self):
        ok, models, _detail = list_available_models(self.session.cfg)
        self.assertTrue(ok)
        self.assertEqual(models, ["compatible-test-model"])
        self.assertEqual(self.requests[0][:2], ("/v1/models", "Bearer fixture-only-key"))
        cancel = threading.Event()
        with closing(self.runtime().stream("interrupt stream", tokens=True, cancel=cancel)) as events:
            for event in events:
                if event.kind == "token":
                    cancel.set()
        self.assertTrue(cancel.is_set())
        with closing(self.runtime().stream("continue stream", tokens=True)) as events:
            result = list(events)
        self.assertEqual("".join(event.value for event in result if event.kind == "token"), "compatible streaming reply")
        self.assertEqual([event.value.content for event in result if event.kind == "message"
                          and isinstance(event.value, AIMessage)], ["compatible streaming reply"])
        path, authorization, body = self.requests[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(authorization, "Bearer fixture-only-key")
        self.assertEqual(body["model"], "compatible-test-model")
        self.assertTrue(body["stream"])
        self.assertEqual({tool["function"]["name"] for tool in body["tools"]}, {"current_time", "system_info"})

    def test_cli_redirected_stream_and_nonstream_render_once_and_keep_history(self):
        output = io.StringIO()
        with patch.object(cli, "console", Console(file=output, width=120, force_terminal=False)):
            self.session.chat_turn("redirected stream")
            self.session.cfg.cli.stream = False
            self.session.chat_turn("nonstream")
            self.session.handle_command("/status")
        self.assertEqual(output.getvalue().count("compatible streaming reply"), 1)
        self.assertEqual(output.getvalue().count("nonstream reply"), 1)
        self.assertNotIn("fixture-only-key", output.getvalue())
        self.assertEqual(self.session.repository.messages(self.session.thread_id)[-1].content, "nonstream reply")
        self.assertFalse(self.requests[-1][2].get("stream", False))
        self.assertTrue(ping_model(self.session.cfg)[0])


if __name__ == "__main__":
    unittest.main()
