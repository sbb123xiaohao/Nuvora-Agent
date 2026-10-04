"""共享 Agent 运行时：图执行、消息事件和中断检查点修复。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Iterator, Literal

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage

from .agent import build_agent
from .config import Config
from .messages import content_text


@dataclass(frozen=True)
class RuntimeEvent:
    kind: Literal["token", "message"]
    value: str | BaseMessage


class AgentRuntime:
    def __init__(self, cfg: Config, model, memory, checkpointer, *, workspace, thread_id: str):
        self.graph = build_agent(cfg, model, memory, checkpointer, workspace=workspace)
        self.config = {"configurable": {"thread_id": thread_id},
                       "recursion_limit": cfg.agent.max_iterations * 2 + 10}

    def _reconcile_interruption(self) -> None:
        """只提交最后一次模型步骤，避免旧工具结果改变当前恢复节点。"""
        snapshot = self.graph.get_state(self.config)
        messages = snapshot.values.get("messages", [])
        boundary = next((i for i in range(len(messages) - 1, -1, -1)
                         if isinstance(messages[i], HumanMessage)), -1)
        current = messages[boundary + 1:]
        last_ai = next((m for m in reversed(current) if isinstance(m, AIMessage)), None)
        if last_ai is None:
            return
        if not last_ai.tool_calls:
            # get_state 会合并 pending_writes；显式提交后仓库读取同一份历史。
            self.graph.update_state(self.config, {"messages": [last_ai]}, as_node="model")
            return
        call_ids = {c["id"] for c in last_ai.tool_calls}
        results = [m for m in current if isinstance(m, ToolMessage) and m.tool_call_id in call_ids]
        answered = {m.tool_call_id for m in results}
        pending = [ToolMessage(content="用户已停止此回合。", tool_call_id=c["id"], name=c["name"], status="error")
                   for c in last_ai.tool_calls if c["id"] not in answered]
        self.graph.update_state(self.config, {"messages": [last_ai, *results, *pending]}, as_node="tools")

    def stream(self, text: str, *, cancel: Event | None = None, tokens=False,
               streaming=True) -> Iterator[RuntimeEvent]:
        cancel = cancel if cancel is not None else Event()
        iterator = None
        interrupted = True
        try:
            if cancel.is_set():
                interrupted = False  # 尚未提交输入，无需修改历史。
                return
            inputs = {"messages": [("user", text)]}
            if not streaming:
                before = self.graph.get_state(self.config)
                count = len(before.values.get("messages", []))
                state = self.graph.invoke(inputs, config=self.config)
                for message in state.get("messages", [])[count:]:
                    if cancel.is_set():
                        break
                    yield RuntimeEvent("message", message)
            else:
                modes = ["messages", "updates"] if tokens else ["updates"]
                iterator = self.graph.stream(inputs, config=self.config, stream_mode=modes)
                for mode, chunk in iterator:
                    if cancel.is_set():
                        break
                    if mode == "messages":
                        message, _metadata = chunk
                        if isinstance(message, (AIMessage, AIMessageChunk)):
                            token = content_text(message.content)
                            if token:
                                yield RuntimeEvent("token", token)
                    else:
                        for update in chunk.values():
                            if not isinstance(update, dict):
                                continue
                            messages = update.get("messages", [])
                            if not isinstance(messages, list):
                                messages = [messages]
                            for message in messages:
                                yield RuntimeEvent("message", message)
            interrupted = cancel.is_set()
        finally:
            try:
                if iterator is not None:
                    iterator.close()
            finally:
                if interrupted:
                    # 不覆盖模型/传输/KeyboardInterrupt 的原始异常。
                    try:
                        self._reconcile_interruption()
                    except Exception:
                        pass
