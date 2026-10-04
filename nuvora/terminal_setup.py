"""终端配置向导：输入与展示留在入口层，保存复用 ConfigStore。"""

from __future__ import annotations

import os
import sys

from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt

from .config import ENV_FIELDS, Config, ConfigError, ConfigStore
from .llm import list_available_models, redact_error


def configure(store: ConfigStore, console) -> Config | None:
    if not sys.stdin.isatty():
        console.print("配置向导需要交互终端。请运行 python -m nuvora configure，或使用 NUVORA_* 环境变量。", markup=False)
        return None
    cfg = store.snapshot()
    console.print("\n模型配置：回车保留当前值；API Key 不回显。", markup=False)
    warning = store.public_state()["config_warning"]
    if warning:
        console.print(warning, markup=False)
    payload = {"model": {}}
    draft = cfg
    try:
        for field, label in (("base_url", "接口地址"), ("api_key", "API Key（留空保留，输入 - 清除）")):
            env_name = ENV_FIELDS[field]
            if os.environ.get(env_name, "").strip():
                console.print(f"{label.split('（', 1)[0]}：由 {env_name} 覆盖，保留环境变量设置。", markup=False)
                continue
            if field == "api_key":
                value = Prompt.ask(label, default="", password=True, show_default=False, console=console)
                if value.strip() == "-":
                    payload["clear_api_key"] = True
                else:
                    payload["model"][field] = value
            else:
                payload["model"][field] = Prompt.ask(label, default=cfg.model.base_url, console=console)
        # 用草稿探测，不提前保存，也不显示密钥。
        draft = store.candidate(payload)
        if Confirm.ask("获取端点的可用模型列表", default=False, console=console):
            ok, models, detail = list_available_models(draft)
            console.print(redact_error(draft, detail), markup=False)
            if ok:
                for model in models[:50]:
                    console.print("  " + model, markup=False)
                if len(models) > 50:
                    console.print("仅显示前 50 个；可输入任意完整模型名称。", markup=False)
        if os.environ.get(ENV_FIELDS["model"], "").strip():
            console.print("模型名称：由 NUVORA_MODEL 覆盖，保留环境变量设置。", markup=False)
        else:
            payload["model"]["model"] = Prompt.ask("模型名称", default=cfg.model.model, console=console)
        if Confirm.ask("设置工具开关和高级参数", default=False, console=console):
            payload["tools"] = {
                "web_enabled": Confirm.ask("启用联网工具", default=cfg.tools.web_enabled, console=console),
                "files_enabled": Confirm.ask("启用文件工具", default=cfg.tools.files_enabled, console=console),
                "python_enabled": Confirm.ask("启用 Python 隔离工具", default=cfg.tools.python_enabled, console=console),
                "python_timeout": IntPrompt.ask("Python 超时（1–120 秒）", default=cfg.tools.python_timeout, console=console),
                "web_search_max_results": IntPrompt.ask("搜索条数（1–20）", default=cfg.tools.web_search_max_results, console=console),
            }
            payload["memory"] = {"enabled": Confirm.ask("启用长期记忆", default=cfg.memory.enabled, console=console)}
            payload["cli"] = {"stream": Confirm.ask("逐消息输出", default=cfg.cli.stream, console=console)}
            payload["model"]["temperature"] = FloatPrompt.ask("温度（0–2）", default=cfg.model.temperature, console=console)
            payload["agent"] = {"max_iterations": IntPrompt.ask("工具轮数（1–1000）", default=cfg.agent.max_iterations, console=console)}
        draft = store.candidate(payload)
        if not draft.model.model:
            raise ConfigError("模型名称不能为空；可用 /models 探测，或向服务提供商确认名称。")
        console.print(f"\n接口：{draft.model.base_url}\n模型：{draft.model.model}\nAPI Key：{'已设置' if draft.model.api_key else '未设置'}", markup=False)
        if not Confirm.ask("保存设置", default=True, console=console):
            console.print("已取消保存。", markup=False)
            return None
        store.update(payload)
        console.print(f"设置已保存到 {store.path}", markup=False)
        return store.snapshot()
    except (KeyboardInterrupt, EOFError):
        console.print("\n已取消配置。", markup=False)
    except (ConfigError, OSError) as error:
        detail = redact_error(cfg, redact_error(draft, error))
        entered_key = payload["model"].get("api_key", "").strip()
        if entered_key:
            detail = detail.replace(entered_key, "[已隐藏 API Key]")
        console.print("配置未保存：" + detail, markup=False)
    return None
