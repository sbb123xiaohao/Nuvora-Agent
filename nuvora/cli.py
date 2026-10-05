"""NUVORA 交互终端：chat REPL + doctor / models 子命令。"""

from __future__ import annotations

import sys
from contextlib import ExitStack, closing

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import APP_NAME, TAGLINE, __version__
from .config import CONFIG_PATH, DATA_DIR, WORKSPACE_DIR, Config, ConfigError, ConfigStore, load_config
from .errors import NotFoundError
from .llm import list_available_models, redact_error, run_doctor
from .memory import LongTermMemory
from .runtime import AgentRuntime
from .sessions import SessionRepository
from .terminal_output import TerminalOutput
from .terminal_setup import configure
from .tools import build_tools
from .tools.files import sandbox_list_dir, sandbox_read_file

console = Console()

HELP_ROWS = [
    ("/help", "显示本帮助"),
    ("/setup", "配置模型、密钥、工具与高级参数并保存，下一回合生效"),
    ("/new", "开始一个新会话（历史会话保留，可用 /sessions 查看）"),
    ("/sessions", "列出历史会话"),
    ("/resume <id>", "切换到某个历史会话继续聊"),
    ("/history", "查看当前会话的完整对话记录"),
    ("/model <名称>", "临时切换模型（仅本会话生效，不写回配置文件）"),
    ("/models", "探测当前端点的可用模型列表"),
    ("/tools", "列出 NUVORA 当前装配的所有工具"),
    ("/memory [关键词]", "查看长期记忆（可按关键词检索）"),
    ("/remember <内容>", "保存一条长期记忆"),
    ("/forget <编号>", "删除指定的长期记忆"),
    ("/files [目录]", "浏览工作区文件"),
    ("/read <路径>", "读取工作区文本文件"),
    ("/status", "查看模型、会话、配置与工作区状态"),
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


class ChatSession:
    def __init__(self, cfg: Config):
        self.settings = ConfigStore(CONFIG_PATH, DATA_DIR, cfg)
        self.cfg = self.settings.snapshot()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        self.workspace = WORKSPACE_DIR
        with ExitStack() as resources:
            self.repository = SessionRepository(DATA_DIR)
            resources.callback(self.repository.close)
            self.checkpointer = self.repository.checkpointer
            self.memory = LongTermMemory(DATA_DIR / "memory.db")
            resources.callback(self.memory.close)
            self._resources = resources.pop_all()
        self.thread_id = "default"
        self._models_by_thread: dict[str, str] = {}
        self._closed = False

    @property
    def model_name(self) -> str | None:
        return self._models_by_thread.get(self.thread_id) or self.cfg.model.model or None

    def close(self) -> None:
        if not self._closed:
            try:
                self._resources.close()
            finally:
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
            table.add_column(style="bold cyan", width=22)
            table.add_column()
            for name, desc in HELP_ROWS:
                table.add_row(name, desc)
            console.print(table)
        elif cmd in ("/setup", "/config"):
            self._cmd_setup()
        elif cmd == "/new":
            self.thread_id = self.repository.new()["id"]
            console.print(f"[green]✓[/] 已开启新会话，thread_id = [bold]{self.thread_id}[/]（旧会话仍保留）")
        elif cmd == "/sessions":
            self._cmd_sessions()
        elif cmd == "/resume":
            self._cmd_resume(arg)
        elif cmd == "/history":
            self._cmd_history()
        elif cmd == "/model":
            self._cmd_model(arg)
        elif cmd == "/models":
            self._cmd_models()
        elif cmd == "/tools":
            self._cmd_tools()
        elif cmd == "/memory":
            self._cmd_memory(arg)
        elif cmd == "/remember":
            self._cmd_remember(arg)
        elif cmd == "/forget":
            self._cmd_forget(arg)
        elif cmd == "/files":
            console.print(sandbox_list_dir(self.workspace, arg or "."), markup=False, highlight=False)
        elif cmd == "/read":
            if not arg:
                console.print("用法：/read <工作区内的相对路径>", markup=False)
            else:
                console.print(sandbox_read_file(self.workspace, arg), markup=False, highlight=False)
        elif cmd == "/status":
            self._cmd_status()
        else:
            console.print(f"[yellow]未知命令 {cmd}[/]，输入 /help 查看可用命令")
        return True

    def _cmd_sessions(self):
        try:
            sessions = self.repository.list()
        except Exception as e:  # noqa: BLE001
            console.print("读取会话失败：" + redact_error(self.cfg, e), markup=False)
            return
        if not sessions:
            console.print("（还没有任何会话）")
            return
        table = Table(title="历史会话", box=None)
        table.add_column("thread_id", style="cyan")
        table.add_column("标题")
        table.add_column("备注")
        for session in sessions:
            tid = session["id"]
            table.add_row(Text(tid), Text(session["title"]), "← 当前" if tid == self.thread_id else "")
        console.print(table)
        console.print("[dim]用 /resume <thread_id> 继续某个会话[/]")

    def _cmd_setup(self):
        cfg = configure(self.settings, console)
        if cfg is not None:
            self.cfg = cfg
            self._models_by_thread.pop(self.thread_id, None)
            console.print("[green]✓[/] 下一回合使用已保存的配置。")

    def _cmd_resume(self, arg: str):
        if not arg:
            console.print("用法：/resume <thread_id>（先用 /sessions 查看）")
            return
        try:
            self.repository.messages(arg)
        except NotFoundError:
            console.print(f"没有找到会话 {arg}", markup=False)
            return
        except Exception as e:
            console.print(f"[red]读取会话失败：[/]{redact_error(self.cfg, e)}")
            return
        self.thread_id = arg
        console.print(f"已切换到会话 {arg}，用 /history 查看历史。", markup=False)

    def _cmd_history(self):
        try:
            messages = self.repository.messages(self.thread_id)
        except NotFoundError:
            messages = []
        if not messages:
            console.print("（当前会话还没有消息）", markup=False)
            return
        output = TerminalOutput(console)
        for message in messages:
            output.message(message, include_user=True)

    def _cmd_status(self):
        table = Table(box=None, show_header=False)
        table.add_column(style="dim")
        table.add_column()
        for label, value in (
            ("模型", self.model_name or "未配置"),
            ("接口", self.cfg.model.base_url),
            ("API Key", "已设置" if self.cfg.model.api_key else "未设置"),
            ("会话", self.thread_id),
            ("配置", str(self.settings.path)),
            ("工作区", str(self.workspace)),
            ("流式输出", "开" if self.cfg.cli.stream else "关"),
            ("长期记忆", "开" if self.cfg.memory.enabled else "关"),
        ):
            table.add_row(label, Text(value))
        console.print(table)

    def _cmd_model(self, arg: str):
        if not arg:
            current = self.model_name or "（未设置）"
            console.print(f"当前模型：[bold cyan]{current}[/]（用 /model <名称> 临时切换）")
            return
        self._models_by_thread[self.thread_id] = arg
        console.print(f"[green]✓[/] 本会话模型已切换为 [bold]{arg}[/]（不写回配置文件；用 /setup 保存设置）")

    def _cmd_models(self):
        console.print(f"正在探测 [cyan]{self.cfg.model.base_url or '（未配置 base_url）'}[/] …")
        ok, ids, msg = list_available_models(self.cfg)
        if not ok:
            console.print(Panel(msg, title="探测失败", border_style="yellow"))
            console.print("[dim]提示：用 /setup 手动填写模型名[/]")
            return
        table = Table(title=f"可用模型（{msg}）", box=None)
        table.add_column("#", justify="right", style="dim")
        table.add_column("model id", style="cyan")
        for i, mid in enumerate(ids, 1):
            mark = " ← 当前" if mid == self.model_name else ""
            table.add_row(str(i), mid + mark)
        console.print(table)
        console.print("[dim]临时切换：/model <model id>；持久化：/setup[/]")

    def _cmd_tools(self):
        tools = build_tools(self.cfg, self.memory, self.workspace)
        table = Table(title=f"已装配 {len(tools)} 个工具", box=None)
        table.add_column("工具", style="bold cyan")
        table.add_column("说明")
        for t in tools:
            table.add_row(t.name, t.description.strip().splitlines()[0])
        console.print(table)

    def _cmd_memory(self, query: str):
        if not self.cfg.memory.enabled:
            console.print("[yellow]长期记忆未启用[/]（用 /setup 设置）")
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
            table.add_row(f"#{mid}", created[:16], Text(content), Text(tags))
        console.print(table)

    def _cmd_remember(self, text: str):
        if not self.cfg.memory.enabled:
            console.print("长期记忆未启用（用 /setup 设置）", markup=False)
            return
        if not text:
            console.print("用法：/remember <内容>", markup=False)
            return
        mid = self.memory.add(text, thread_id=self.thread_id)
        console.print(f"已保存记忆 #{mid}", markup=False)

    def _cmd_forget(self, arg: str):
        if not self.cfg.memory.enabled:
            console.print("长期记忆未启用（用 /setup 设置）", markup=False)
            return
        if not arg.lstrip("#").isdigit():
            console.print("用法：/forget <编号>（先用 /memory 查看）", markup=False)
            return
        mid = int(arg.lstrip("#"))
        deleted = self.memory.delete(mid)
        console.print(f"已删除记忆 #{mid}" if deleted else f"没有找到记忆 #{mid}", markup=False)

    # ---------- 对话 ----------

    def _need_model(self) -> bool:
        if not (self.model_name or self.cfg.model.model):
            console.print(
                Panel(
                    "还没有指定模型，先运行 /setup 配置接口、密钥和模型。\n"
                    "也可用 /models 探测名称，再用 /model <id> 临时启用。",
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
            runtime = AgentRuntime(self.cfg, model, self.memory, self.checkpointer,
                                   workspace=self.workspace, thread_id=self.thread_id)
        except Exception as e:  # noqa: BLE001
            console.print(Panel(f"构建 Agent 失败：{redact_error(self.cfg, e)}", border_style="red"))
            return

        try:
            self.repository.record_turn(self.thread_id, user_text)
            # 非终端输出完整消息，交互终端逐 token 更新 Markdown。
            with TerminalOutput(console) as output, closing(runtime.stream(
                user_text, tokens=self.cfg.cli.stream and console.is_terminal,
                streaming=self.cfg.cli.stream,
            )) as events:
                for event in events:
                    output.event(event)
        except KeyboardInterrupt:
            console.print("\n[dim]⏹ 已打断本回合[/]")
        except Exception as e:  # noqa: BLE001
            console.print(Panel(f"{type(e).__name__}: {redact_error(self.cfg, e)}\n\n💡 {_hint_for(e)}", title="出错了", border_style="red"))

    def _build_model(self):
        from .llm import build_chat_model

        return build_chat_model(self.cfg, model_override=self.model_name)

    # ---------- 主循环 ----------

    def run(self):
        try:
            self._run_loop()
        finally:
            self.close()

    def _run_loop(self):
        console.print(f"\n[bold magenta]{APP_NAME}[/] [dim]v{__version__}[/] · 终端 AI 助理")
        console.print(f"模型：{self.model_name or '未配置（/setup 配置）'}", markup=False)
        console.print("[dim]工作区 workspace/ · /help 命令 · /status 状态 · /quit 退出[/]\n")
        if not self.model_name and console.is_terminal:
            self._cmd_setup()
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
    console.print("[dim]运行 python -m nuvora configure 保存模型设置[/]")
    return 0


def _usage() -> None:
    console.print(f"""[bold magenta]{APP_NAME}[/] v{__version__} — {TAGLINE}

[bold]用法[/]：python -m nuvora [命令]

[bold]命令[/]：
  chat             进入交互终端（默认，可不带命令）
  configure        终端配置模型、密钥、工具与高级参数
  doctor [--ping]  环境自检（--ping 额外做一次真实对话验证）
  models           列出当前端点的可用模型
  version          显示版本号""")


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except ConfigError as e:
        console.print(Panel(str(e), title="配置错误", border_style="red"))
        console.print("[dim]运行 python -m nuvora configure 重新配置[/]")
        return 1


def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0].lower() == "chat":
        if len(args) > 1:
            _usage()
            return 2
        ChatSession(load_config()).run()
        return 0

    cmd = args[0].lower()
    if (cmd == "doctor" and args[1:] not in ([], ["--ping"])) or (cmd != "doctor" and len(args) > 1):
        _usage()
        console.print("[yellow]命令参数无效[/]")
        return 2
    if cmd in ("version", "-v", "--version"):
        print(f"{APP_NAME} v{__version__}")
        return 0
    if cmd in ("help", "-h", "--help"):
        _usage()
        return 0
    if cmd == "configure":
        return 0 if configure(ConfigStore(CONFIG_PATH, DATA_DIR), console) is not None else 1

    if cmd not in ("doctor", "models"):
        _usage()
        console.print(f"[yellow]未知命令：{args[0]}[/]")
        return 2

    cfg = load_config()
    if cmd == "doctor":
        return _cmd_doctor(cfg, ping="--ping" in args)
    if cmd == "models":
        return _cmd_models(cfg)
