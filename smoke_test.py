#!/usr/bin/env python3
"""NUVORA 冒烟测试——不消耗 API、不需要 key。

运行：wsl bash -c "cd '<项目目录>' && .venv/bin/python smoke_test.py"
"""

from __future__ import annotations

import shutil
import sys
import traceback
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from nuvora import APP_NAME, SYSTEM_PROMPT_TEMPLATE  # noqa: E402
from nuvora.config import DATA_DIR, WORKSPACE_DIR, load_config  # noqa: E402
from nuvora.llm import list_available_models  # noqa: E402
from nuvora.memory import LongTermMemory, build_memory_tools  # noqa: E402
from nuvora.tools import files, python_repl  # noqa: E402

console = Console()
RESULTS: list[tuple[str, bool | None, str]] = []
TESTS: list[tuple[str, object]] = []


def check(name: str):
    """注册一个测试项，由 main() 统一执行。"""

    def deco(fn):
        TESTS.append((name, fn))
        return fn

    return deco


@check("1. 配置加载")
def t_config():
    cfg = load_config()
    assert cfg.model.base_url, "base_url 不应为空"
    assert cfg.agent.max_iterations > 0 and cfg.tools.python_timeout > 0
    return f"base_url={cfg.model.base_url}"


@check("2. 文件沙箱：正常读写")
def t_sandbox_ok():
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    msg = files.sandbox_write_file(WORKSPACE_DIR, "smoke/hello.txt", "NUVORA 测试内容")
    text = files.sandbox_read_file(WORKSPACE_DIR, "smoke/hello.txt")
    assert "NUVORA 测试内容" in text, "读回内容不一致"
    listing = files.sandbox_list_dir(WORKSPACE_DIR, "smoke")
    assert "hello.txt" in listing
    assert "smoke" in files.sandbox_list_dir(WORKSPACE_DIR), "默认根目录浏览失败"
    return msg


@check("3. 文件沙箱：拦截路径逃逸")
def t_sandbox_escape():
    for bad in ("../outside.txt", "a/../../evil.txt", "/etc/passwd", "C:/Windows/win.ini"):
        try:
            files.resolve_in_sandbox(bad, WORKSPACE_DIR)
        except files.SandboxViolation:
            continue
        raise AssertionError(f"路径未被拦截：{bad}")
    return "4 个逃逸样本全部拦截"


@check("4. 长期记忆：增/查/删")
def t_memory():
    db = DATA_DIR / "_smoke_memory.db"
    if db.exists():
        db.unlink()
    mem = LongTermMemory(db)
    mid = mem.add("用户喜欢用 WSL 跑 Python", tags="偏好")
    mem.add("正在开发 NUVORA 项目", tags="项目")
    hits = mem.search("WSL")
    assert hits and hits[0][0] == mid, "关键词检索失败"
    assert mem.delete(mid), "删除失败"
    assert not mem.search("WSL"), "删除后不应再检索到"
    total = mem.count()
    mem.close()
    db.unlink()
    return f"剩余 {total} 条测试记忆"


@check("5. 长期记忆：LangChain 工具调用")
def t_memory_tools():
    db = DATA_DIR / "_smoke_tools.db"
    if db.exists():
        db.unlink()
    mem = LongTermMemory(db)
    tools = build_memory_tools(mem)
    assert {t.name for t in tools} == {"remember", "recall", "forget"}
    out = tools[0].invoke({"content": "冒烟测试记忆", "tags": "测试"})
    assert "已记住" in out
    hits = mem.search("冒烟")
    out2 = tools[1].invoke({"query": "冒烟"})
    assert "冒烟测试记忆" in out2, "recall 工具检索失败"
    out3 = tools[2].invoke({"memory_id": hits[0][0]})
    assert "已删除" in out3
    mem.close()
    db.unlink()
    return "remember / recall / forget 三个工具均正常"


@check("6. run_python：执行与超时")
def t_python():
    available, detail = python_repl.sandbox_status(WORKSPACE_DIR)
    if not available:
        raise unittest.SkipTest(detail)
    out = python_repl.run_python_code("print(6 * 7)", timeout=10, cwd=WORKSPACE_DIR)
    assert "42" in out, f"执行输出异常：{out}"
    out2 = python_repl.run_python_code("import time; time.sleep(5); print('done')", timeout=1, cwd=WORKSPACE_DIR)
    assert "超时" in out2, "超时未被捕获"
    out3 = python_repl.run_python_code("1/0", timeout=10, cwd=WORKSPACE_DIR)
    assert "ZeroDivisionError" in out3 and "[stderr]" in out3, "异常未被捕获"
    return "正常执行 / 超时终止 / 异常捕获 均正常"


@check("7. 模型发现：不可达端点优雅降级")
def t_models_fallback():
    cfg = load_config()
    cfg.model.api_key = ""  # 本测试绝不发送用户的真实密钥
    cfg.model.base_url = "http://127.0.0.1:9"  # discard 端口，立即拒绝
    ok, ids, msg = list_available_models(cfg, timeout=3)
    assert not ok and not ids and msg, "不可达端点应优雅返回失败信息"
    return f"返回失败信息：{msg[:60]}…"


@check("8. Agent 图构建（不发起网络请求）")
def t_agent_build():
    from nuvora.agent import build_agent, open_checkpointer
    from langchain_core.messages import AIMessage
    from tests.fakes import OfflineModel

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cp_db = DATA_DIR / "_smoke_cp.db"
    saver = open_checkpointer(cp_db)
    cfg = load_config()
    model = OfflineModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "list_dir", "args": {}, "id": "smoke-list", "type": "tool_call"}]),
        AIMessage(content="offline smoke done"),
    ])
    try:
        agent = build_agent(cfg, model, None, saver)
        config = {"configurable": {"thread_id": "smoke"}}
        result = agent.invoke({"messages": [("user", "list workspace")]}, config)
        assert result["messages"][-1].content == "offline smoke done"
        assert saver.get_tuple(config) is not None
        tools_count = len(agent.get_graph().nodes)
    finally:
        saver.conn.close()
    cp_db.unlink(missing_ok=True)
    return f"图构建、工具循环与 SQLite 持久化成功（{tools_count} 个节点）"


@check("9. 系统提示词与人格")
def t_prompt():
    from nuvora.agent import build_system_prompt

    cfg = load_config()
    prompt = build_system_prompt(cfg, None)
    assert APP_NAME in prompt and "workspace/" in prompt and "web_search" in prompt
    assert "长期记忆" in prompt
    assert SYSTEM_PROMPT_TEMPLATE.strip().startswith("你是")
    return f"提示词 {len(prompt)} 字符，包含人格/环境/工具守则"


@check("10. 依赖完整性")
def t_deps():
    import ddgs  # noqa: F401
    import httpx  # noqa: F401
    import langchain  # noqa: F401
    import langchain_openai  # noqa: F401
    import langgraph  # noqa: F401
    import rich  # noqa: F401
    import trafilatura  # noqa: F401
    from langchain.agents import create_agent  # noqa: F401
    from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: F401

    return "langchain / langgraph / langchain-openai / ddgs / trafilatura / rich / httpx 全部可导入"


def _run_tests() -> int:
    console.print(f"\n[bold magenta]{APP_NAME}[/] 冒烟测试（无 API 消耗）\n")
    for name, fn in TESTS:
        try:
            detail = fn() or ""
            RESULTS.append((name, True, detail))
            console.print(f"[green]✓[/] {name} [dim]{detail}[/]")
        except unittest.SkipTest as e:
            RESULTS.append((name, None, str(e)))
            console.print(f"[yellow]↷[/] {name} [dim]跳过：{e}[/]")
        except Exception as e:  # noqa: BLE001
            RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
            console.print(f"[red]✗[/] {name} → {type(e).__name__}: {e}")
            console.print(f"[dim]{traceback.format_exc(limit=3)}[/]")
    passed = sum(1 for _, ok, _ in RESULTS if ok is True)
    failed = sum(1 for _, ok, _ in RESULTS if ok is False)
    skipped = sum(1 for _, ok, _ in RESULTS if ok is None)

    table = Table(title="结果汇总")
    table.add_column("状态", justify="center")
    table.add_column("数量", justify="right")
    table.add_row("[green]通过[/]", str(passed))
    table.add_row("[red]失败[/]", str(failed))
    table.add_row("[yellow]跳过[/]", str(skipped))
    console.print(table)

    # 清理沙箱测试残留
    shutil.rmtree(WORKSPACE_DIR / "smoke", ignore_errors=True)

    return 0 if failed == 0 else 1


def main() -> int:
    global DATA_DIR, WORKSPACE_DIR
    import nuvora.tools as registry

    RESULTS.clear()
    with tempfile.TemporaryDirectory(prefix="nuvora-smoke-") as temp:
        DATA_DIR = Path(temp) / "data"
        WORKSPACE_DIR = Path(temp) / "workspace"
        with patch.object(registry, "WORKSPACE_DIR", WORKSPACE_DIR):
            return _run_tests()


if __name__ == "__main__":
    sys.exit(main())
