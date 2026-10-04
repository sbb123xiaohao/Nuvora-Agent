"""NUVORA 工具注册表：集中装配，供 agent 与 /tools 命令使用。"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from ..config import Config, WORKSPACE_DIR
from ..memory import LongTermMemory, build_memory_tools
from . import files, python_repl, system, web


def _make_web_search(cfg: Config) -> BaseTool:
    from langchain_core.tools import tool

    @tool
    def web_search(query: str) -> str:
        """联网搜索。查询时事、新闻、价格、软件版本等任何时效性信息时使用。
        query 为搜索关键词，建议精炼。返回标题、链接与摘要列表。"""
        return web.do_web_search(cfg, query)

    return web_search


def _make_web_fetch() -> BaseTool:
    from langchain_core.tools import tool

    @tool
    def web_fetch(url: str) -> str:
        """抓取一个网页并提取正文（Markdown 文本）。用于阅读 web_search
        结果中的原文，或用户给出的具体链接。仅支持 http/https。"""
        return web.do_web_fetch(url)

    return web_fetch


def _make_list_dir() -> BaseTool:
    from langchain_core.tools import tool

    @tool
    def list_dir(path: str = ".") -> str:
        """列出 workspace/ 沙箱内的目录内容。path 为相对 workspace/ 的
        路径，默认列出根目录。"""
        return files.sandbox_list_dir(WORKSPACE_DIR, path)

    return list_dir


def _make_read_file() -> BaseTool:
    from langchain_core.tools import tool

    @tool
    def read_file(path: str) -> str:
        """读取 workspace/ 沙箱内的一个文本文件。path 为相对 workspace/
        的路径，如 "notes/todo.md"。超出上限会截断。"""
        return files.sandbox_read_file(WORKSPACE_DIR, path)

    return read_file


def _make_write_file() -> BaseTool:
    from langchain_core.tools import tool

    @tool
    def write_file(path: str, content: str) -> str:
        """把文本内容写入 workspace/ 沙箱内的文件（覆盖式）。path 为相对
        workspace/ 的路径，父目录不存在会自动创建。"""
        return files.sandbox_write_file(WORKSPACE_DIR, path, content)

    return write_file


def _make_run_python(cfg: Config) -> BaseTool:
    from langchain_core.tools import tool

    @tool
    def run_python(code: str) -> str:
        """在 Linux/WSL 的操作系统隔离中执行 Python，返回 stdout/stderr。
        适合计算、数据处理、日期推算、格式转换、生成文件到 workspace/ 等。
        禁止联网；隔离不可用时拒绝执行。代码用 print() 输出结果。
        标准库可用，第三方库不一定已安装。"""
        return python_repl.run_python_code(code, cfg.tools.python_timeout, WORKSPACE_DIR)

    return run_python


def _make_current_time() -> BaseTool:
    from langchain_core.tools import tool

    @tool
    def current_time() -> str:
        """获取当前的本地时间与 UTC 时间。需要准确时间/日期时使用。"""
        return system.current_time_text()

    return current_time


def _make_system_info() -> BaseTool:
    from langchain_core.tools import tool

    @tool
    def system_info() -> str:
        """获取运行环境信息：操作系统、Python 版本、项目目录等。"""
        from ..config import PROJECT_ROOT

        return system.system_info_text(PROJECT_ROOT)

    return system_info


def build_tools(cfg: Config, memory: LongTermMemory | None = None) -> list[BaseTool]:
    tools: list[BaseTool] = [
        _make_web_search(cfg),
        _make_web_fetch(),
        _make_list_dir(),
        _make_read_file(),
        _make_write_file(),
        _make_run_python(cfg),
        _make_current_time(),
        _make_system_info(),
    ]
    if memory is not None:
        tools.extend(build_memory_tools(memory))
    return tools
