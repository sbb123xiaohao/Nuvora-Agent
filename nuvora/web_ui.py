"""本地统一界面：配置、持久会话、真实 Agent 流和长期记忆。"""

from __future__ import annotations

import argparse
import copy
import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from . import APP_NAME, __version__
from .agent import build_agent, list_thread_ids, open_checkpointer
from .config import CONFIG_PATH, DATA_DIR, WORKSPACE_DIR, Config, ConfigError, _apply, load_config, save_config, validate_config
from .llm import build_chat_model, list_available_models, ping_model, redact_error
from .memory import LongTermMemory
from .tools.python_repl import sandbox_status
from .tools import files

ASSET_DIR = Path(__file__).with_name("static")
MAX_BODY = 131072
ENV_FIELDS = {"base_url": "NUVORA_BASE_URL", "api_key": "NUVORA_API_KEY", "model": "NUVORA_MODEL"}


class UIError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block if isinstance(block, str) else str(block.get("text", ""))
            for block in content if isinstance(block, (str, dict))
        )
    return str(content or "")


def public_messages(messages) -> list[dict]:
    rows = []
    for message in messages:
        if isinstance(message, HumanMessage):
            rows.append({"role": "user", "text": content_text(message.content), "id": message.id})
        elif isinstance(message, AIMessage):
            text = content_text(message.content)
            calls = [{"id": c.get("id"), "name": c.get("name"), "args": c.get("args", {})}
                     for c in message.tool_calls]
            if text or calls:
                rows.append({"role": "assistant", "text": text, "calls": calls, "id": message.id})
        elif isinstance(message, ToolMessage):
            rows.append({"role": "tool", "text": content_text(message.content)[:20000],
                         "name": message.name, "call_id": message.tool_call_id, "id": message.id})
    return rows


class WebApplication:
    def __init__(self, cfg: Config | None = None, *, data_dir=DATA_DIR, workspace=WORKSPACE_DIR,
                 config_path=CONFIG_PATH, model_factory=build_chat_model):
        self.lock = threading.RLock()
        self.turn_lock = threading.Lock()
        self.cancel = threading.Event()
        self.busy = False
        self.closed = False
        self.closing = False
        self.config_path = Path(config_path)
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_warning = ""
        try:
            self.cfg = cfg or load_config(self.config_path)
        except ConfigError as error:
            self.cfg = Config()
            self.config_warning = str(error) + "。请在右侧重新填写并保存。"
        self.model_factory = model_factory
        self.checkpointer = open_checkpointer(self.data_dir / "checkpoints.db")
        self.memory = LongTermMemory(self.data_dir / "memory.db")
        self.db = sqlite3.connect(self.data_dir / "interface.db", check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, updated TEXT NOT NULL)")
        self.db.commit()
        self.active_thread = ""
        self.csrf = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        self.python_available, self.python_detail = sandbox_status(self.workspace)
        sessions = self.sessions()
        if sessions:
            self.active_thread = sessions[0]["id"]
        else:
            self.new_session()

    def _idle(self):
        if self.busy:
            raise UIError("请先停止或等待当前回复完成，再修改设置。", 409)
        if self.closing:
            raise UIError("界面正在关闭。", 503)

    def _touch(self, thread_id: str, title: str | None = None):
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        old = self.db.execute("SELECT title FROM sessions WHERE id=?", (thread_id,)).fetchone()
        label = title or (old[0] if old else "历史会话")
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, updated=excluded.updated",
                            (thread_id, label[:80], now))

    def sessions(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id,title,updated FROM sessions ORDER BY updated DESC").fetchall()
            result = [{"id": r[0], "title": r[1], "updated": r[2]} for r in rows]
            known = {r["id"] for r in result}
            for tid in list_thread_ids(self.checkpointer):
                if tid not in known:
                    result.append({"id": tid, "title": "历史会话 · " + tid[:8], "updated": ""})
            return result

    def new_session(self) -> dict:
        with self.lock:
            self._idle()
            self.active_thread = uuid.uuid4().hex
            self._touch(self.active_thread, "新对话")
            return {"id": self.active_thread, "title": "新对话"}

    def history(self, thread_id: str) -> list[dict]:
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 200:
            raise UIError("会话编号无效。")
        with self.lock:
            checkpoint = self.checkpointer.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
            if checkpoint is None:
                exists = self.db.execute("SELECT 1 FROM sessions WHERE id=?", (thread_id,)).fetchone()
                if not exists:
                    raise UIError("没有找到此会话。", 404)
                return []
            return public_messages(checkpoint.checkpoint.get("channel_values", {}).get("messages", []))

    def state(self) -> dict:
        with self.lock:
            cfg = asdict(self.cfg)
            model = cfg["model"]
            model["api_key_configured"] = bool(model.pop("api_key"))
            allowed = {name: cfg[name] for name in ("model", "agent", "tools", "memory", "cli")}
            return {"version": __version__, "config": allowed, "config_warning": self.config_warning,
                    "env_overrides": [field for field, name in ENV_FIELDS.items() if os.environ.get(name, "").strip()],
                    "busy": self.busy, "active_thread": self.active_thread, "sessions": self.sessions(),
                    "python": {"available": self.python_available, "detail": self.python_detail},
                    "memory_count": self.memory.count(), "workspace": str(self.workspace),
                    "csrf_token": self.csrf}

    def candidate(self, payload: dict) -> Config:
        if not isinstance(payload, dict):
            raise UIError("配置必须是对象。")
        cfg = copy.deepcopy(self.cfg)
        for name in ("model", "agent", "tools", "memory", "cli"):
            raw = payload.get(name, {})
            if not isinstance(raw, dict):
                raise UIError("配置分组必须是对象。")
            if name == "model":
                raw = dict(raw)
                if "api_key" in raw and not isinstance(raw["api_key"], str):
                    raise UIError("密钥必须是文本。")
                # 密钥输入留空表示保留；清除必须由单独操作明确要求。
                if not raw.get("api_key"):
                    raw.pop("api_key", None)
            _apply(getattr(cfg, name), raw)
        if payload.get("clear_api_key") is True:
            cfg.model.api_key = ""
        cfg.model.base_url = cfg.model.base_url.strip().rstrip("/")
        cfg.model.model = cfg.model.model.strip()
        cfg.model.api_key = cfg.model.api_key.strip()
        if len(cfg.model.model) > 300 or len(cfg.model.base_url) > 2048 or len(cfg.model.api_key) > 4096:
            raise UIError("模型配置内容过长。")
        validate_config(cfg)
        return cfg

    def update_config(self, payload: dict) -> dict:
        with self.lock:
            self._idle()
            cfg = self.candidate(payload)
            for field, name in ENV_FIELDS.items():
                value = os.environ.get(name, "").strip()
                expected = value.rstrip("/") if field == "base_url" else value
                if value and getattr(cfg.model, field) != expected:
                    raise UIError(f"{name} 正在覆盖此项，请先移除该环境变量再保存。")
            if self.config_warning and self.config_path.exists():
                backup = self.data_dir / ("config-backup-" + uuid.uuid4().hex + ".toml")
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(self.config_path.read_bytes())
            save_config(cfg, self.config_path)
            cfg.config_path = self.config_path
            cfg.missing_file = cfg.using_example = False
            self.cfg = cfg
            self.config_warning = ""
            return self.state()

    def begin_turn(self, payload: dict):
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise UIError("请输入 1–32000 字符的消息。")
        with self.lock:
            self._idle()
            tid = payload.get("thread_id", self.active_thread)
            self.history(tid)
            if not self.cfg.model.model:
                raise UIError("先在右侧选择或填写模型，然后保存。")
            if not self.turn_lock.acquire(blocking=False):
                raise UIError("已有回复正在生成。", 409)
            self.busy = True
            self.cancel.clear()
            self.active_thread = tid
            try:
                old = self.db.execute("SELECT title FROM sessions WHERE id=?", (tid,)).fetchone()
                self._touch(tid, " ".join(text.split())[:40] if not old or old[0] == "新对话" else None)
            except Exception:
                self.busy = False
                self.turn_lock.release()
                raise
            return tid, text.strip(), copy.deepcopy(self.cfg)

    def stop(self) -> dict:
        self.cancel.set()
        return {"requested": self.busy}

    def finish_turn(self):
        with self.lock:
            self.busy = False
            self.turn_lock.release()
            if self.closing:
                self._close_databases()

    def _complete_pending_tools(self, agent, config):
        snapshot = agent.get_state(config)
        messages = snapshot.values.get("messages", [])
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        pending = [ToolMessage(content="用户已停止此回合。", tool_call_id=c["id"], name=c["name"], status="error")
                   for m in messages if isinstance(m, AIMessage) for c in m.tool_calls if c["id"] not in answered]
        # 停止时工具可能已完成并写入 pending_writes，但主检查点尚未提交。
        # 合并已有结果和取消结果，确保下一回合与界面读取同一份完整历史。
        calls = {c["id"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
        results = [m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id in calls]
        if results or pending:
            agent.update_state(config, {"messages": [*results, *pending]}, as_node="tools")
        elif messages and isinstance(messages[-1], AIMessage):
            agent.update_state(config, {"messages": messages}, as_node="model")

    def events(self, turn):
        tid, text, cfg = turn
        graph = None
        config = {"configurable": {"thread_id": tid}, "recursion_limit": cfg.agent.max_iterations * 2 + 10}
        try:
            # 全部真实模型调用都使用已保存的配置。
            model = self.model_factory(cfg)
            graph = build_agent(cfg, model, self.memory if cfg.memory.enabled else None, self.checkpointer, workspace=self.workspace)
            yield "start", {"thread_id": tid}
            if cfg.cli.stream:
                stream = graph.stream({"messages": [("user", text)]}, config=config, stream_mode=["messages", "updates"])
            else:
                stream = graph.stream({"messages": [("user", text)]}, config=config, stream_mode=["updates"])
            try:
                for mode, chunk in stream:
                    if self.cancel.is_set():
                        break
                    if mode == "messages":
                        message, _metadata = chunk
                        if isinstance(message, (AIMessage, AIMessageChunk)):
                            token = content_text(message.content)
                            if token:
                                yield "token", {"text": token}
                    elif mode == "updates":
                        for update in chunk.values():
                            if not isinstance(update, dict):
                                continue
                            messages = update.get("messages", [])
                            if not isinstance(messages, list):
                                messages = [messages]
                            for message in messages:
                                if isinstance(message, AIMessage):
                                    for call in message.tool_calls:
                                        yield "tool_start", {"id": call["id"], "name": call["name"], "args": call.get("args", {})}
                                elif isinstance(message, ToolMessage):
                                    yield "tool_result", {"id": message.tool_call_id, "name": message.name,
                                                         "text": content_text(message.content)[:20000]}
            finally:
                stream.close()
            if self.cancel.is_set():
                self._complete_pending_tools(graph, config)
            yield "stopped" if self.cancel.is_set() else "done", {"thread_id": tid, "messages": self.history(tid)}
        except Exception as error:
            if graph is not None:
                try:
                    self._complete_pending_tools(graph, config)
                except Exception:
                    pass
            yield "error", {"message": redact_error(cfg, error)[:2000]}
        except GeneratorExit:
            self.cancel.set()
            if graph is not None:
                try:
                    self._complete_pending_tools(graph, config)
                except Exception:
                    pass
            raise

    def close(self):
        with self.lock:
            self.closing = True
            self.cancel.set()
            if not self.busy:
                self._close_databases()

    def _close_databases(self):
        if not self.closed:
            self.checkpointer.conn.close()
            self.memory.close()
            self.db.close()
            self.closed = True


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app: WebApplication, port: int = 8765):
        self.app = app
        super().__init__(("127.0.0.1", port), WebHandler)


class WebHandler(BaseHTTPRequestHandler):
    server_version = "Nuvora"

    def log_message(self, *_args):
        pass

    def _headers(self, status: int, mime: str, *, length: int | None = None, cookie=False):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if cookie:
            self.send_header("Set-Cookie", "nuvora_session=" + self.server.app.cookie + "; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", length=len(body))
        self.wfile.write(body)

    def _authorized(self, mutation=False):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            raise UIError("访问地址无效。", 403)
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in allowed}:
            raise UIError("不允许跨站访问。", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise UIError("不允许跨站访问。", 403)
        if urlsplit(self.path).path.startswith("/api/"):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                raise UIError("请重新打开界面。", 403) from None
            entry = cookie.get("nuvora_session")
            if entry is None or not hmac.compare_digest(entry.value, self.server.app.cookie):
                raise UIError("请重新打开界面。", 403)
            if mutation and not hmac.compare_digest(self.headers.get("X-Nuvora-CSRF", ""), self.server.app.csrf):
                raise UIError("请求验证失败，请刷新界面。", 403)

    def _body(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise UIError("请求须使用 JSON。", 415)
        if self.headers.get("Transfer-Encoding"):
            raise UIError("不支持分块请求。")
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise UIError("缺少请求长度。", 411) from None
        if size < 0 or size > MAX_BODY:
            raise UIError("请求内容过大。", 413)
        self.connection.settimeout(15)
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise UIError("请求内容不完整。")
            body = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError):
            raise UIError("JSON 格式无效。") from None
        if not isinstance(body, dict):
            raise UIError("请求必须是对象。")
        return body

    def _error(self, error):
        self._json({"error": redact_error(self.server.app.cfg, error)[:2000]}, getattr(error, "status", 400))

    def do_GET(self):
        try:
            self._authorized()
            parsed = urlsplit(self.path)
            if parsed.path == "/api/state":
                self._json(self.server.app.state())
            elif parsed.path == "/api/session":
                tid = parse_qs(parsed.query).get("id", [""])[0]
                self._json({"messages": self.server.app.history(tid)})
            elif parsed.path == "/api/memory":
                query = parse_qs(parsed.query).get("q", [""])[0][:500]
                rows = self.server.app.memory.search(query, 100) if query else self.server.app.memory.recent(100)
                self._json({"items": [{"id": mid, "created": created, "content": content, "tags": tags}
                                     for mid, created, content, tags in rows]})
            elif parsed.path == "/api/files":
                path = parse_qs(parsed.query).get("path", ["."])[0]
                text = files.sandbox_list_dir(self.server.app.workspace, path)
                if text.startswith("文件操作失败"):
                    raise UIError(text)
                self._json({"text": text})
            elif parsed.path == "/api/file":
                path = parse_qs(parsed.query).get("path", [""])[0]
                text = files.sandbox_read_file(self.server.app.workspace, path)
                if text.startswith("文件操作失败"):
                    raise UIError(text)
                if text.startswith(("（文件不存在", "（这是一个目录")):
                    raise UIError(text, 404)
                self._json({"text": text, "truncated": text.endswith("字符，已截断）")})
            else:
                assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "application/javascript"),
                          "/style.css": ("style.css", "text/css")}
                if parsed.path not in assets:
                    raise UIError("页面不存在。", 404)
                name, mime = assets[parsed.path]
                body = (ASSET_DIR / name).read_bytes()
                self._headers(200, mime + "; charset=utf-8", length=len(body), cookie=parsed.path == "/")
                self.wfile.write(body)
        except (UIError, ConfigError) as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self._json({"error": "读取失败，请刷新后重试。"}, 500)

    def do_POST(self):
        streaming = False
        turn = None
        iterator = None
        app = self.server.app
        try:
            self._authorized(mutation=True)
            body = self._body()
            path = urlsplit(self.path).path
            if path == "/api/config":
                self._json(app.update_config(body))
            elif path == "/api/session":
                self._json(app.new_session())
            elif path in ("/api/models", "/api/test"):
                with app.lock:
                    cfg = app.candidate(body)
                if path == "/api/models":
                    ok, models, detail = list_available_models(cfg)
                    self._json({"ok": ok, "models": models[:2000], "detail": redact_error(cfg, detail)})
                else:
                    if not cfg.model.model:
                        raise UIError("先选择或填写模型，再测试对话连接。")
                    ok, detail = ping_model(cfg)
                    self._json({"ok": ok, "detail": redact_error(cfg, detail)})
            elif path == "/api/stop":
                self._json(app.stop())
            elif path == "/api/memory":
                with app.lock:
                    app._idle()
                    action = body.get("action")
                    if action == "add":
                        content, tags = body.get("content"), body.get("tags", "")
                        if not isinstance(content, str) or not isinstance(tags, str):
                            raise UIError("记忆内容和标签必须是文本。")
                        self._json({"id": app.memory.add(content, tags)})
                    elif action == "delete" and type(body.get("id")) is int:
                        self._json({"deleted": app.memory.delete(body["id"])})
                    else:
                        raise UIError("记忆操作无效。")
            elif path == "/api/file":
                with app.lock:
                    app._idle()
                    path, text = body.get("path"), body.get("text")
                    if not isinstance(path, str) or not isinstance(text, str) or len(text) > 40000:
                        raise UIError("请填写相对路径，文件内容上限 40000 字符。")
                    result = files.sandbox_write_file(app.workspace, path, text)
                    if result.startswith("文件操作失败"):
                        raise UIError(result)
                    self._json({"detail": result})
            elif path == "/api/chat":
                turn = app.begin_turn(body)
                self._headers(200, "text/event-stream; charset=utf-8")
                streaming = True
                iterator = app.events(turn)
                for name, event in iterator:
                    data = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(("event: " + name + "\ndata: " + data + "\n\n").encode("utf-8"))
                    self.wfile.flush()
            else:
                raise UIError("接口不存在。", 404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            if turn is not None:
                app.cancel.set()
        except Exception as error:
            if not streaming:
                self._error(error)
        finally:
            if iterator is not None:
                iterator.close()
            if turn is not None:
                app.finish_turn()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NUVORA 统一操作界面")
    parser.add_argument("--port", type=int, default=8765, help="本地端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="仅启动服务，不自动打开浏览器")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口须在 0–65535 之间")
    app = WebApplication()
    try:
        server = WebServer(app, args.port)
    except OSError:
        app.close()
        print("端口被占用，请关闭已打开的 NUVORA，或使用 --port 指定另一端口。")
        return 1
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"{APP_NAME} v{__version__} · {url}")
    print("在浏览器中聊天和配置模型。关闭本窗口或按 Ctrl+C 结束服务。")
    if not args.no_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0
