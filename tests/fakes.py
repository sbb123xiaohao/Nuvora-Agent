"""离线脚本模型：执行真实 Agent 图，但不访问 API。"""

import os

# 测试进程不向外发送 LangSmith 跟踪；不影响真实对话入口。
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGSMITH_TRACING_V2"] = "false"

from pydantic import Field
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class OfflineModel(BaseChatModel):
    responses: list[AIMessage]
    index: int = 0
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "nuvora-offline-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        response = self.responses[self.index]
        self.index += 1
        return ChatResult(generations=[ChatGeneration(message=response)])
