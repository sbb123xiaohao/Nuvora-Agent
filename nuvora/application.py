"""应用服务：协调配置、会话、工作区及单个运行回合，不依赖 HTTP。"""

from __future__ import annotations

import threading
from contextlib import ExitStack
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

from . import __version__
from .config import CONFIG_PATH, DATA_DIR, WORKSPACE_DIR, Config, ConfigStore
from .errors import ApplicationError, BusyError, ClosedError, NotFoundError
from .llm import build_chat_model, list_available_models, ping_model, redact_error
from .memory import LongTermMemory
from .messages import content_text, public_messages
from .runtime import AgentRuntime
from .sessions import SessionRepository
from .tools import files
from .tools.python_repl import sandbox_status


class Turn:
    """拥有一次回合的取消信号和迭代器；退出上下文总会释放占用。"""

    def __init__(self, owner: AgentApplication, thread_id: str, text: str, cfg: Config):
        self.thread_id, self.text, self.cfg = thread_id, text, cfg
        self.cancel = threading.Event()
        self._owner = owner
        self._iterator = None
        self._closed = self._completed = False

    def __enter__(self) -> Turn:
        if self._closed:
            raise ClosedError("此回合已结束。")
        return self

    def __exit__(self, *_exc):
        self.close()

    def events(self):
        if self._closed:
            raise ClosedError("此回合已结束。")
        if self._iterator is not None:
            raise BusyError("此回合已经开始执行。")
        self._iterator = self._owner._events(self)
        return self._iterator

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._completed:
            self.cancel.set()
        try:
            if self._iterator is not None:
                self._iterator.close()
        finally:
            self._owner._finish_turn(self)


class AgentApplication:
    def __init__(self, cfg: Config | None = None, *, data_dir=DATA_DIR, workspace=WORKSPACE_DIR,
                 config_path=CONFIG_PATH, model_factory=build_chat_model):
        self._lock = threading.RLock()
        self._active_turn: Turn | None = None
        self._closed = self._closing = False
        self.data_dir, self.workspace = Path(data_dir), Path(workspace)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.settings = ConfigStore(Path(config_path), self.data_dir, cfg)
        self.model_factory = model_factory
        with ExitStack() as resources:
            self.repository = SessionRepository(self.data_dir)
            resources.callback(self.repository.close)
            self.memory = LongTermMemory(self.data_dir / "memory.db")
            resources.callback(self.memory.close)
            self.python_available, self.python_detail = sandbox_status(self.workspace)
            sessions = self.repository.list()
            self.active_thread = sessions[0]["id"] if sessions else self.repository.new()["id"]
            self._resources = resources.pop_all()

    @property
    def cfg(self) -> Config:
        return self.settings.snapshot()

    @property
    def checkpointer(self):
        return self.repository.checkpointer

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active_turn is not None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _ensure_open(self) -> None:
        if self._closing:
            raise ClosedError("应用正在关闭。")

    def _idle(self) -> None:
        self._ensure_open()
        if self._active_turn is not None:
            raise BusyError("请先停止或等待当前回复完成，再修改设置。")

    def sessions(self) -> list[dict]:
        with self._lock:
            self._ensure_open()
            return self.repository.list()

    def new_session(self) -> dict:
        with self._lock:
            self._idle()
            session = self.repository.new()
            self.active_thread = session["id"]
            return session

    def history(self, thread_id: str) -> list[dict]:
        with self._lock:
            self._ensure_open()
            return public_messages(self.repository.messages(thread_id))

    def state(self) -> dict:
        with self._lock:
            self._ensure_open()
            return {"version": __version__, **self.settings.public_state(),
                    "busy": self._active_turn is not None, "active_thread": self.active_thread,
                    "sessions": self.repository.list(),
                    "python": {"available": self.python_available, "detail": self.python_detail},
                    "memory_count": self.memory.count(), "workspace": str(self.workspace)}

    def update_config(self, payload: dict) -> dict:
        with self._lock:
            self._idle()
            self.settings.update(payload)
            return self.state()

    def candidate(self, payload: dict) -> Config:
        with self._lock:
            self._ensure_open()
            return self.settings.candidate(payload)

    def discover_models(self, payload: dict) -> dict:
        cfg = self.candidate(payload)
        # 网络请求使用独立草稿，不持有应用锁，也不写入配置。
        ok, models, detail = list_available_models(cfg)
        return {"ok": ok, "models": models[:2000], "detail": redact_error(cfg, detail)}

    def test_connection(self, payload: dict) -> dict:
        cfg = self.candidate(payload)
        if not cfg.model.model:
            raise ApplicationError("先选择或填写模型，再测试对话连接。")
        ok, detail = ping_model(cfg)
        return {"ok": ok, "detail": redact_error(cfg, detail)}

    def redact(self, error) -> str:
        return redact_error(self.settings.snapshot(), error)[:2000]

    def begin_turn(self, payload: dict) -> Turn:
        if not isinstance(payload, dict):
            raise ApplicationError("消息必须是对象。")
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ApplicationError("请输入 1–32000 字符的消息。")
        with self._lock:
            self._idle()
            tid = payload.get("thread_id", self.active_thread)
            self.repository.messages(tid)  # 校验存在，包含尚未发送消息的新会话。
            cfg = self.settings.snapshot()
            if not cfg.model.model:
                raise ApplicationError("先在右侧选择或填写模型，然后保存。")
            self.repository.record_turn(tid, text)
            turn = Turn(self, tid, text.strip(), cfg)
            self._active_turn, self.active_thread = turn, tid
            return turn

    def stop(self) -> dict:
        with self._lock:
            if self._active_turn is None:
                return {"requested": False}
            self._active_turn.cancel.set()
            return {"requested": True}

    def _finish_turn(self, turn: Turn) -> None:
        with self._lock:
            # 旧回合重复关闭不能释放后来启动的回合。
            if self._active_turn is not turn:
                return
            self._active_turn = None
            if self._closing:
                self._close_resources()

    def _events(self, turn: Turn):
        iterator = None
        try:
            model = self.model_factory(turn.cfg)
            runtime = AgentRuntime(turn.cfg, model, self.memory, self.checkpointer,
                                   workspace=self.workspace, thread_id=turn.thread_id)
            yield "start", {"thread_id": turn.thread_id}
            iterator = runtime.stream(turn.text, cancel=turn.cancel, tokens=turn.cfg.cli.stream)
            for event in iterator:
                if event.kind == "token":
                    yield "token", {"text": event.value}
                elif isinstance(event.value, AIMessage):
                    for call in event.value.tool_calls:
                        yield "tool_start", {"id": call["id"], "name": call["name"], "args": call.get("args", {})}
                elif isinstance(event.value, ToolMessage):
                    message = event.value
                    yield "tool_result", {"id": message.tool_call_id, "name": message.name,
                                          "text": content_text(message.content)[:20000]}
            rows = public_messages(self.repository.messages(turn.thread_id))
            turn._completed = True
            yield "stopped" if turn.cancel.is_set() else "done", {"thread_id": turn.thread_id, "messages": rows}
        except Exception as error:
            turn._completed = True
            yield "error", {"message": redact_error(turn.cfg, error)[:2000]}
        finally:
            if iterator is not None:
                iterator.close()

    def list_memory(self, query: str = "") -> dict:
        with self._lock:
            self._ensure_open()
            rows = self.memory.search(query[:500], 100) if query else self.memory.recent(100)
            return {"items": [{"id": mid, "created": created, "content": content, "tags": tags}
                              for mid, created, content, tags in rows]}

    def update_memory(self, payload: dict) -> dict:
        with self._lock:
            self._idle()
            action = payload.get("action")
            if action == "add":
                content, tags = payload.get("content"), payload.get("tags", "")
                if not isinstance(content, str) or not isinstance(tags, str):
                    raise ApplicationError("记忆内容和标签必须是文本。")
                try:
                    return {"id": self.memory.add(content, tags)}
                except ValueError as error:
                    raise ApplicationError(str(error)) from None
            if action == "delete" and type(payload.get("id")) is int:
                return {"deleted": self.memory.delete(payload["id"])}
            raise ApplicationError("记忆操作无效。")

    def list_files(self, path: str = ".") -> dict:
        with self._lock:
            self._ensure_open()
            text = files.sandbox_list_dir(self.workspace, path)
            if text.startswith("文件操作失败"):
                raise ApplicationError(text)
            return {"text": text}

    def read_file(self, path: str) -> dict:
        with self._lock:
            self._ensure_open()
            text = files.sandbox_read_file(self.workspace, path)
            if text.startswith("文件操作失败"):
                raise ApplicationError(text)
            if text.startswith(("（文件不存在", "（这是一个目录")):
                raise NotFoundError(text)
            return {"text": text, "truncated": text.endswith("字符，已截断）")}

    def save_file(self, payload: dict) -> dict:
        with self._lock:
            self._idle()
            path, text = payload.get("path"), payload.get("text")
            if not isinstance(path, str) or not isinstance(text, str) or len(text) > 40000:
                raise ApplicationError("请填写相对路径，文件内容上限 40000 字符。")
            detail = files.sandbox_write_file(self.workspace, path, text)
            if detail.startswith("文件操作失败"):
                raise ApplicationError(detail)
            return {"detail": detail}

    def close(self) -> None:
        with self._lock:
            self._closing = True
            if self._active_turn is not None:
                self._active_turn.cancel.set()
            else:
                self._close_resources()

    def _close_resources(self) -> None:
        if not self._closed:
            try:
                self._resources.close()
            finally:
                self._closed = True
