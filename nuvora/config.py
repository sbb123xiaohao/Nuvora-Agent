"""NUVORA 配置加载：config.toml + 环境变量覆盖。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
CONFIG_EXAMPLE_PATH = PROJECT_ROOT / "config.example.toml"
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
DATA_DIR = PROJECT_ROOT / "data"


@dataclass
class ModelConfig:
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    api_key: str = ""
    model: str = ""
    temperature: float = 0.7


@dataclass
class AgentConfig:
    max_iterations: int = 30


@dataclass
class ToolsConfig:
    python_timeout: int = 30
    web_search_max_results: int = 5


@dataclass
class MemoryConfig:
    enabled: bool = True


@dataclass
class CliConfig:
    stream: bool = True


@dataclass
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    cli: CliConfig = field(default_factory=CliConfig)
    config_path: Path | None = None
    using_example: bool = False
    missing_file: bool = False


def _apply(section: object, raw: dict) -> None:
    """把 TOML 里的字段写入 dataclass，忽略未知键，类型尽量兼容。"""
    valid = {f.name for f in fields(section)}
    for key, value in raw.items():
        if key not in valid:
            continue
        current = getattr(section, key)
        try:
            if isinstance(current, bool):
                setattr(section, key, bool(value))
            elif isinstance(current, int):
                setattr(section, key, int(value))
            elif isinstance(current, float):
                setattr(section, key, float(value))
            else:
                setattr(section, key, str(value))
        except (TypeError, ValueError):
            pass  # 类型不对就保留默认值


def load_config() -> Config:
    cfg = Config()
    path = CONFIG_PATH if CONFIG_PATH.exists() else None
    if path is None:
        cfg.missing_file = True
        if CONFIG_EXAMPLE_PATH.exists():
            path = CONFIG_EXAMPLE_PATH
            cfg.using_example = True
    if path is not None:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        if isinstance(raw.get("model"), dict):
            _apply(cfg.model, raw["model"])
        if isinstance(raw.get("agent"), dict):
            _apply(cfg.agent, raw["agent"])
        if isinstance(raw.get("tools"), dict):
            _apply(cfg.tools, raw["tools"])
        if isinstance(raw.get("memory"), dict):
            _apply(cfg.memory, raw["memory"])
        if isinstance(raw.get("cli"), dict):
            _apply(cfg.cli, raw["cli"])
        cfg.config_path = path

    # 环境变量覆盖（非空才覆盖）
    for env_name, target, attr in (
        ("NUVORA_API_KEY", cfg.model, "api_key"),
        ("NUVORA_BASE_URL", cfg.model, "base_url"),
        ("NUVORA_MODEL", cfg.model, "model"),
    ):
        value = os.environ.get(env_name)
        if value:
            setattr(target, attr, value.strip())

    cfg.model.base_url = (cfg.model.base_url or "").strip().rstrip("/")
    cfg.model.api_key = (cfg.model.api_key or "").strip()
    cfg.model.model = (cfg.model.model or "").strip()
    return cfg
