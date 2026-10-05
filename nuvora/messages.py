"""将模型的字符串或多模态文本块转换为终端文本。"""


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block if isinstance(block, str) else str(block.get("text") or block.get("content") or "")
            for block in content if isinstance(block, (str, dict))
        )
    return str(content or "")
