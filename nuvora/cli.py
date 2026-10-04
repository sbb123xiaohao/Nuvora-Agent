"""NUVORA 交互终端：chat REPL + doctor / models 子命令。"""

from __future__ import annotations

import json
import sys
import uuid

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import APP_NAME, TAGLINE, __version__
from .agent import build_agent, list_thread_ids, open_checkpointer
from .config import DATA_DIR, WORKSPACE_DIR, Config, ConfigError, load_config
from .llm import list_available_models, redact_error, run_doctor
from .memory import LongTermMemory
from .tools import build_tools

console = Console()

HELP_ROWS = [
    ("/help", "显示本帮助"),
    ("/new", "开始一个新会话（历史会话保留，可用 /sessions 查看）"),
    ("/sessions", "列出历史会话"),
    ("/resume <id>", "切换到某个历史会话继续聊"),
    ("/model <名称>", "临时切换模型（仅本会话生效，不写回配置文件）"),
    ("/models", "探测当前端点的可用模型列表"),
    ("/tools", "列出 NUVORA 当前装配的所有工具"),
    ("/memory [关键词]", "查看长期记忆（可按关键词检索）"),
    ("/quit", "退出（/exit 等效）"),
]

ERROR_HINTS = [
    (("401", "Unauthorized", "invalid", "Incorrect API key"), "API Key 可能无效，检查 config.toml 的 api_key"),
    (("404", "not found", "Not Found"), "模型名或接口路径不对：用 /models 核对模型名；检查 base_url 是否正确（多数端点需要以 /v1 结尾）"),
    (("429", "rate"), "触发限流，稍等片刻再试"),
    (("ConnectError", "Timeout", "timed out", "Connection"), "连不上端点：检查网络/VPN，以及 base_url 是否可达"),
    (("Recursion", "recursion"), "达到单回合工具调用轮数上限，可在 config.toml 调大 [agent] max_iterations"),
]


def _hint_for(error: Exception) -> str:
    text = str(error)
    for keys, hint in ERROR_HINTS:
        if any(k.lower() in text.lower() for k in keys):
            return hint
    return "可运行 `python -m nuvora doctor --ping` 自检排查"


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content or "")


class ChatSession:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        self.memory = LongTermMemory(DATA_DIR / "memory.db") if cfg.memory.enabled else None
        self.checkpointer = open_checkpointer(DATA_DIR / "checkpoints.db")
        self.thread_id = "default"
        self._models_by_thread: dict[str, str] = {}
        self._closed = False

    @property
    def model_name(self) -> str | None:
        return self._models_by_thread.get(self.thread_id) or self.cfg.model.model or None

    def close(self) -> None:
        if not self._closed:
            try:
                self.checkpointer.conn.close()
            finally:
                if self.memory is not None:
                    self.memory.close()
                self._closed = True

    # ---------- 命令 ----------

    def handle_command(self, line: str) -> bool:
        """处理斜杠命令。返回 False 表示要退出。"""
        parts = line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            return False
        if cmd == "/help":
            table = Table(box=None, show_header=False, pad_edge=False)
            table.add_column(style="bold cyan", width=16)
            table.add_column()
            for name, desc in HELP_ROWS:
                table.add_row(name, desc)
            console.print(table)
        elif cmd == "/new":
            self.thread_id = uuid.uuid4().hex
            console.print(f"[green]✓[/] 已开启新会话，thread_id = [bold]{self.thread_id}[/]（旧会话仍保留）")
        elif cmd == "/sessions":
            self._cmd_sessions()
        elif cmd == "/resume":
            self._cmd_resume(arg)
        elif cmd == "/model":
            self._cmd_model(arg)
        elif cmd == "/models":
            self._cmd_models()
        elif cmd == "/tools":
            self._cmd_tools()
        elif cmd == "/memory":
            self._cmd_memory(arg)
        else:
            console.print(f"[yellow]未知命令 {cmd}[/]，输入 /help 查看可用命令")
        return True

    def _cmd_sessions(self):
        ids: list[str] = []
        try:
            ids = list_thread_ids(self.checkpointer)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]读取会话失败：[/]{e}")
            return
        if not ids:
            console.print("（还没有任何会话）")
            return
        table = Table(title="历史会话", box=None)
        table.add_column("thread_id", style="cyan")
        table.add_column("备注")
        for tid in ids:
            table.add_row(tid, "← 当前" if tid == self.thread_id else "")
        console.print(table)
        console.print("[dim]用 /resume <thread_id> 继续某个会话[/]")

    def _cmd_resume(self, arg: str):
        if not arg:
            console.print("用法：/resume <thread_id>（先用 /sessions 查看）")
            return
        try:
            checkpoint = self.checkpointer.get_tuple({"configurable": {"thread_id": arg, "checkpoint_ns": ""}})
        except Exception as e:
            console.print(f"[red]读取会话失败：[/]{redact_error(self.cfg, e)}")
            return
        if checkpoint is not None:
            self.thread_id = arg
            console.print(f"[green]✓[/] 已切换到会话 [bold]{arg}[/]，历史上下文已恢复")
            return
        console.print(f"[yellow]没有找到会话 {arg}[/]")

    def _cmd_model(self, arg: str):
        if not arg:
            current = self.model_name or "（未设置）"
            console.print(f"当前模型：[bold cyan]{current}[/]（用 /model <名称> 临时切换）")
            return
        self._models_by_thread[self.thread_id] = arg
        console.print(f"[green]✓[/] 本会话模型已切换为 [bold]{arg}[/]（不写回配置文件；要持久化请编辑 config.toml）")

    def _cmd_models(self):
        console.print(f"正在探测 [cyan]{self.cfg.model.base_url or '（未配置 base_url）'}[/] …")
        ok, ids, msg = list_available_models(self.cfg)
        if not ok:
            console.print(Panel(msg, title="探测失败", border_style="yellow"))
            console.print("[dim]提示：也可以直接在 config.toml 手动填写模型名[/]")
            return
        table = Table(title=f"可用模型（{msg}）", box=None)
        table.add_column("#", justify="right", style="dim")
        table.add_column("model id", style="cyan")
        for i, mid in enumerate(ids, 1):
            mark = " ← 当前" if mid == self.model_name else ""
            table.add_row(str(i), mid + mark)
        console.print(table)
        console.print("[dim]临时切换：/model <model id>；持久化：编辑 config.toml 的 model 字段[/]")

    def _cmd_tools(self):
        tools = build_tools(self.cfg, self.memory)
        table = Table(title=f"已装配 {len(tools)} 个工具", box=None)
        table.add_column("工具", style="bold cyan")
        table.add_column("说明")
        for t in tools:
            table.add_row(t.name, t.description.strip().splitlines()[0])
        console.print(table)

    def _cmd_memory(self, query: str):
        if self.memory is None:
            console.print("[yellow]长期记忆未启用[/]（config.toml [memory] enabled = false）")
            return
        rows = self.memory.search(query) if query.strip() else self.memory.recent(10)
        if not rows:
            console.print("（没有找到相关记忆）")
            return
        table = Table(box=None)
        table.add_column("#", justify="right", style="dim")
        table.add_column("时间", style="dim")
        table.add_column("内容")
        table.add_column("标签", style="dim")
        for mid, created, content, tags in rows:
            table.add_row(f"#{mid}", created[:16], content, tags)
        console.print(table)

    # ---------- 对话 ----------

    def _need_model(self) -> bool:
        if not (self.model_name or self.cfg.model.model):
            console.print(
                Panel(
                    "还没有指定模型，无法对话。两种办法：\n"
                    "1. 运行 /models 探测端点可用模型 → /model <id> 临时启用\n"
                    "2. 编辑 config.toml，填写 [model] model 字段（持久生效）",
                    title="缺少模型配置",
                    border_style="yellow",
                )
            )
            return False
        return True

    def chat_turn(self, user_text: str):
        if not self._need_model():
            return
        try:
            model = self._build_model()
            agent = build_agent(self.cfg, model, self.memory, self.checkpointer)
        except Exception as e:  # noqa: BLE001
            console.print(Panel(f"构建 Agent 失败：{redact_error(self.cfg, e)}", border_style="red"))
            return

        config = {
            "configurable": {"thread_id": self.thread_id},
            "recursion_limit": self.cfg.agent.max_iterations * 2 + 10,
        }
        try:
            snapshot = agent.get_state(config)
            printed = len(snapshot.values.get("messages", [])) if snapshot.values else 0
        except Exception:  # noqa: BLE001
            printed = 0

        try:
            if not self.cfg.cli.stream:
                state = agent.invoke({"messages": [("user", user_text)]}, config=config)
                for msg in state.get("messages", [])[printed:]:
                    self._render_message(msg)
                return
            for state in agent.stream(
                {"messages": [("user", user_text)]},
                config=config,
                stream_mode="values",
            ):
                messages = state.get("messages", [])
                for msg in messages[printed:]:
                    self._render_message(msg)
                printed = len(messages)
        except KeyboardInterrupt:
            console.print("\n[dim]⏹ 已打断本回合[/]")
        except Exception as e:  # noqa: BLE001
            console.print(Panel(f"{type(e).__name__}: {redact_error(self.cfg, e)}\n\n💡 {_hint_for(e)}", title="出错了", border_style="red"))

    def _build_model(self):
        from .llm import build_chat_model

        return build_chat_model(self.cfg, model_override=self.model_name)

    def _render_message(self, msg):
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        if isinstance(msg, HumanMessage):
            return
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                args_str = json.dumps(tc.get("args", {}), ensure_ascii=False)
                if len(args_str) > 300:
                    args_str = args_str[:300] + "…"
                console.print(
                    Panel(args_str, title=f"🛠 调用工具 · {tc.get('name', '?')}", border_style="dim cyan", expand=False)
                )
            text = _content_text(msg.content).strip()
            if text:
                console.print()
                console.print(Panel(Markdown(text), title=f"✦ {APP_NAME}", title_align="left", border_style="magenta"))
                console.print()
        elif isinstance(msg, ToolMessage):
            text = _content_text(msg.content).strip()
            if len(text) > 500:
                text = text[:500] + "…（结果已截断显示）"
            console.print(
                Panel(text or "（空结果）", title=f"↳ {getattr(msg, 'name', 'tool')}", border_style="dim green", expand=False)
            )

    # ---------- 主循环 ----------

    def run(self):
        try:
            self._run_loop()
        finally:
            self.close()

    def _run_loop(self):
        model_status = self.model_name or "[yellow]未配置模型（/models 探测，或编辑 config.toml）[/]"
        console.print(
            Panel(
                f"[bold magenta]{APP_NAME}[/] [dim]v{__version__}[/] · {TAGLINE}\n"
                f"模型：{model_status}\n"
                f"沙箱：workspace/ · 记忆：{'开' if self.memory else '关'} · 会话：{self.thread_id}\n\n"
                f"[dim]输入 /help 查看命令，/quit 退出[/]",
                border_style="magenta",
            )
        )
        while True:
            try:
                line = console.input("[bold cyan]你 › [/]")
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]再见，NUVORA 随时待命 ✦[/]")
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                try:
                    keep_running = self.handle_command(line)
                except Exception as e:
                    console.print(Panel(redact_error(self.cfg, e), title="命令执行失败", border_style="red"))
                    continue
                if not keep_running:
                    console.print("[dim]再见，NUVORA 随时待命 ✦[/]")
                    break
                continue
            self.chat_turn(line)


# ---------- 子命令 ----------


def _cmd_doctor(cfg: Config, ping: bool) -> int:
    checks = run_doctor(cfg, ping=ping)
    table = Table(title=f"{APP_NAME} 环境自检", box=None)
    table.add_column("检查项", style="bold")
    table.add_column("状态", justify="center")
    table.add_column("说明", overflow="fold")
    style_map = {"ok": "green", "warn": "yellow", "fail": "red"}
    icon_map = {"ok": "✓", "warn": "!", "fail": "✗"}
    for name, status, detail in checks:
        table.add_row(name, f"[{style_map[status]}]{icon_map[status]} {status}[/]", detail)
    console.print(table)
    has_fail = any(s == "fail" for _, s, _ in checks)
    return 1 if has_fail else 0


def _cmd_models(cfg: Config) -> int:
    ok, ids, msg = list_available_models(cfg)
    if not ok:
        console.print(Panel(msg, title=f"探测失败 · {cfg.model.base_url}", border_style="yellow"))
        return 1
    console.print(f"[green]✓[/] {cfg.model.base_url} → {msg}")
    for mid in ids:
        console.print(f"  • [cyan]{mid}[/]")
    console.print("[dim]把模型 id 填入 config.toml 的 model 字段即可使用[/]")
    return 0


def _usage() -> None:
    console.print(f"""[bold magenta]{APP_NAME}[/] v{__version__} — {TAGLINE}

[bold]用法[/]：.venv/bin/python -m nuvora [命令]

[bold]命令[/]：
  chat             进入交互对话（默认，可不带命令）
  doctor [--ping]  环境自检（--ping 额外做一次真实对话验证）
  models           列出当前端点的可用模型
  version          显示版本号""")


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except ConfigError as e:
        console.print(Panel(str(e), title="配置错误", border_style="red"))
        return 1


def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] == "chat":
        ChatSession(load_config()).run()
        return 0

    cmd = args[0].lower()
    if cmd in ("version", "-v", "--version"):
        print(f"{APP_NAME} v{__version__}")
        return 0
    if cmd in ("help", "-h", "--help"):
        _usage()
        return 0

    cfg = load_config()
    if cmd == "doctor":
        return _cmd_doctor(cfg, ping="--ping" in args)
    if cmd == "models":
        return _cmd_models(cfg)

    _usage()
    console.print(f"[yellow]未知命令：{args[0]}[/]")
    return 2
