"""网页和终端共用的消息文本及展示投影。"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block if isinstance(block, str) else str(block.get("text") or block.get("content") or "")
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
