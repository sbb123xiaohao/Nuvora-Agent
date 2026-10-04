"""本地网页传输层：静态资源、HTTP 校验、路由和 SSE。"""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import threading
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import APP_NAME, __version__
from .application import AgentApplication
from .config import ConfigError
from .errors import ApplicationError, BusyError, ClosedError, NotFoundError

ASSET_DIR = Path(__file__).with_name("static")
MAX_BODY = 131072
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "application/javascript"),
          "/style.css": ("style.css", "text/css")}
# 保留旧导入入口；应用实现已独立于网页协议。
WebApplication = AgentApplication


class UIError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app: AgentApplication, port: int = 8765):
        self.app = app
        self.csrf = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), WebHandler)

    def public_state(self, state: dict | None = None) -> dict:
        return {**(self.app.state() if state is None else state), "csrf_token": self.csrf}


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
            self.send_header("Set-Cookie", "nuvora_session=" + self.server.cookie + "; HttpOnly; SameSite=Strict; Path=/")
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
            if entry is None or not hmac.compare_digest(entry.value, self.server.cookie):
                raise UIError("请重新打开界面。", 403)
            if mutation and not hmac.compare_digest(self.headers.get("X-Nuvora-CSRF", ""), self.server.csrf):
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
        status = getattr(error, "status", None)
        if status is None:
            status = next((code for kind, code in ((BusyError, 409), (ClosedError, 503), (NotFoundError, 404))
                           if isinstance(error, kind)), 400)
        self._json({"error": self.server.app.redact(error)}, status)

    def do_GET(self):
        try:
            self._authorized()
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            app = self.server.app
            routes = {
                "/api/state": self.server.public_state,
                "/api/session": lambda: {"messages": app.history(query.get("id", [""])[0])},
                "/api/memory": lambda: app.list_memory(query.get("q", [""])[0]),
                "/api/files": lambda: app.list_files(query.get("path", ["."])[0]),
                "/api/file": lambda: app.read_file(query.get("path", [""])[0]),
            }
            if parsed.path in routes:
                self._json(routes[parsed.path]())
            else:
                if parsed.path not in ASSETS:
                    raise UIError("页面不存在。", 404)
                name, mime = ASSETS[parsed.path]
                body = (ASSET_DIR / name).read_bytes()
                self._headers(200, mime + "; charset=utf-8", length=len(body), cookie=parsed.path == "/")
                self.wfile.write(body)
        except (UIError, ConfigError, ApplicationError) as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self._json({"error": "读取失败，请刷新后重试。"}, 500)

    def _chat(self, body: dict):
        # 上下文同时拥有回合与流，写响应/关闭迭代器失败也会释放占用。
        with self.server.app.begin_turn(body) as turn:
            self._streaming = True
            self._headers(200, "text/event-stream; charset=utf-8")
            for name, event in turn.events():
                data = json.dumps(event, ensure_ascii=False)
                self.wfile.write(("event: " + name + "\ndata: " + data + "\n\n").encode("utf-8"))
                self.wfile.flush()

    def do_POST(self):
        self._streaming = False
        try:
            self._authorized(mutation=True)
            body = self._body()
            path = urlsplit(self.path).path
            app = self.server.app
            routes = {
                "/api/config": lambda: self.server.public_state(app.update_config(body)),
                "/api/session": app.new_session,
                "/api/models": lambda: app.discover_models(body),
                "/api/test": lambda: app.test_connection(body),
                "/api/stop": app.stop,
                "/api/memory": lambda: app.update_memory(body),
                "/api/file": lambda: app.save_file(body),
            }
            if path == "/api/chat":
                self._chat(body)
            elif path in routes:
                self._json(routes[path]())
            else:
                raise UIError("接口不存在。", 404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except (UIError, ConfigError, ApplicationError) as error:
            if not self._streaming:
                self._error(error)
        except Exception:
            if not self._streaming:
                self._json({"error": "操作失败，请稍后重试。"}, 500)


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
