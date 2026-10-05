"""终端消息展示：Markdown 流、工具记录与历史输出。"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from . import APP_NAME
from .messages import content_text


class TerminalOutput:
    def __init__(self, console):
        self.console = console
        self._live = None
        self._chunks: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._finish_stream()

    def _finish_stream(self, final_text: str | None = None):
        if self._live is not None:
            try:
                if final_text is not None:
                    self._live.update(Markdown(final_text))
            finally:
                self._live.stop()
                self._live = None
                self._chunks.clear()
                self.console.print()

    def event(self, event):
        if event.kind == "message":
            self.message(event.value)
        elif event.kind == "token":
            if self._live is None:
                self.console.print(f"[bold magenta]{APP_NAME}[/]")
                self._live = Live(Markdown(""), console=self.console, refresh_per_second=8,
                                  vertical_overflow="visible")
                self._live.start()
            self._chunks.append(event.value)
            self._live.update(Markdown("".join(self._chunks)))

    def message(self, msg, *, include_user=False):
        if isinstance(msg, HumanMessage):
            if include_user:
                self.console.print("[bold cyan]你[/]")
                self.console.print(content_text(msg.content), markup=False, highlight=False)
                self.console.print()
        elif isinstance(msg, AIMessage):
            text = content_text(msg.content).strip()
            streamed = self._live is not None
            self._finish_stream(text)
            if text and not streamed:
                self.console.print(f"[bold magenta]{APP_NAME}[/]")
                self.console.print(Markdown(text))
                self.console.print()
            for call in msg.tool_calls:
                args = json.dumps(call.get("args", {}), ensure_ascii=False)
                self.console.print(Panel(Text(args[:300] + ("…" if len(args) > 300 else "")),
                                         title=Text("工具 · " + call.get("name", "?")),
                                         border_style="dim cyan", expand=False))
        elif isinstance(msg, ToolMessage):
            text = content_text(msg.content).strip()
            if len(text) > 500:
                text = text[:500] + "…（结果已截断显示）"
            self.console.print(Panel(Text(text or "（空结果）"), title=Text("结果 · " + (msg.name or "tool")),
                                     border_style="dim green", expand=False))
