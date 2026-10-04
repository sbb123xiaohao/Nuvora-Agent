"""NUVORA 配置加载：config.toml + 环境变量覆盖。"""

from __future__ import annotations

import os
import copy
import math
import tomllib
import json
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
CONFIG_EXAMPLE_PATH = PROJECT_ROOT / "config.example.toml"
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_SECTIONS = ("model", "agent", "tools", "memory", "cli")
ENV_FIELDS = {"base_url": "NUVORA_BASE_URL", "api_key": "NUVORA_API_KEY", "model": "NUVORA_MODEL"}


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
    web_enabled: bool = True
    files_enabled: bool = True
    python_enabled: bool = True


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


class ConfigError(ValueError):
    """配置无效；错误消息不包含密钥或原始配置内容。"""


def _apply(section: object, raw: dict) -> None:
    """加载已知键并校验类型，避免把字符串 false 当成 True。"""
    valid = {f.name for f in fields(section)}
    for key, value in raw.items():
        if key not in valid:
            continue
        current = getattr(section, key)
        if isinstance(current, bool):
            valid_type = type(value) is bool
        elif isinstance(current, int):
            valid_type = type(value) is int
        elif isinstance(current, float):
            valid_type = type(value) in (int, float)
        else:
            valid_type = isinstance(value, str)
        if not valid_type:
            raise ConfigError(f"配置项 {type(section).__name__}.{key} 类型不正确")
        setattr(section, key, float(value) if isinstance(current, float) else value)


def validate_config(cfg: Config) -> None:
    if any(type(value) is not bool for value in (
        cfg.tools.web_enabled, cfg.tools.files_enabled, cfg.tools.python_enabled,
        cfg.memory.enabled, cfg.cli.stream,
    )):
        raise ConfigError("工具、记忆和流式输出开关必须是布尔值")
    if any(not isinstance(value, str) for value in (
        cfg.model.base_url, cfg.model.api_key, cfg.model.model,
    )):
        raise ConfigError("模型地址、密钥和模型名称必须是文本")
    if type(cfg.model.temperature) not in (int, float):
        raise ConfigError("model.temperature 必须是数字")
    for name, value, low, high in (
        ("agent.max_iterations", cfg.agent.max_iterations, 1, 1000),
        ("tools.python_timeout", cfg.tools.python_timeout, 1, 120),
        ("tools.web_search_max_results", cfg.tools.web_search_max_results, 1, 20),
    ):
        if type(value) is not int or not low <= value <= high:
            raise ConfigError(f"{name} 必须是 {low}–{high} 之间的整数")
    if not math.isfinite(cfg.model.temperature) or not 0 <= cfg.model.temperature <= 2:
        raise ConfigError("model.temperature 必须是 0–2 之间的有限数值")
    try:
        url = urlsplit(cfg.model.base_url)
        url.port  # 同时校验端口是否合法
    except ValueError:
        raise ConfigError("model.base_url 不是有效的 HTTP/HTTPS 地址") from None
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ConfigError("model.base_url 必须是无内嵌凭据的 HTTP/HTTPS 地址")


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    target = Path(path) if path is not None else CONFIG_PATH
    path = target if target.exists() else None
    if path is None:
        cfg.missing_file = True
        if CONFIG_EXAMPLE_PATH.exists():
            path = CONFIG_EXAMPLE_PATH
            cfg.using_example = True
    if path is not None:
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError:
            raise ConfigError("配置文件 TOML 格式无效，请检查引号、表名和字段类型") from None
        except OSError as e:
            raise ConfigError(f"无法读取配置文件：{type(e).__name__}") from None
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
        if value and value.strip():
            setattr(target, attr, value.strip())

    cfg.model.base_url = (cfg.model.base_url or "").strip().rstrip("/")
    cfg.model.api_key = (cfg.model.api_key or "").strip()
    cfg.model.model = (cfg.model.model or "").strip()
    validate_config(cfg)
    return cfg


def save_config(cfg: Config, path: Path | None = None) -> None:
    """验证后原子保存；私密配置不进入源码包。"""
    validate_config(cfg)
    target = Path(path) if path is not None else CONFIG_PATH
    sections = []
    for name in CONFIG_SECTIONS:
        section = getattr(cfg, name)
        lines = [f"[{name}]"]
        for item in fields(section):
            value = getattr(section, item.name)
            if isinstance(value, str):
                encoded = json.dumps(value, ensure_ascii=False)
            elif type(value) is bool:
                encoded = "true" if value else "false"
            else:
                encoded = str(value)
            lines.append(f"{item.name} = {encoded}")
        sections.append("\n".join(lines))
    text = "# NUVORA：通过界面保存的配置，请勿提交含密钥的配置文件。\n\n" + "\n\n".join(sections) + "\n"
    tomllib.loads(text)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".nuvora-config-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def patch_config(cfg: Config, payload: dict) -> Config:
    """生成独立且经过校验的草稿；空密钥保留，明确清除才删除。"""
    if not isinstance(payload, dict):
        raise ConfigError("配置必须是对象。")
    result = copy.deepcopy(cfg)
    for name in CONFIG_SECTIONS:
        raw = payload.get(name, {})
        if not isinstance(raw, dict):
            raise ConfigError("配置分组必须是对象。")
        if name == "model":
            raw = dict(raw)
            if "api_key" in raw and not isinstance(raw["api_key"], str):
                raise ConfigError("密钥必须是文本。")
            if not raw.get("api_key"):
                raw.pop("api_key", None)
        _apply(getattr(result, name), raw)
    if payload.get("clear_api_key") is True:
        result.model.api_key = ""
    result.model.base_url = result.model.base_url.strip().rstrip("/")
    result.model.model = result.model.model.strip()
    result.model.api_key = result.model.api_key.strip()
    if len(result.model.model) > 300 or len(result.model.base_url) > 2048 or len(result.model.api_key) > 4096:
        raise ConfigError("模型配置内容过长。")
    validate_config(result)
    return result


class ConfigStore:
    """配置所有权：快照、草稿、脱敏视图及原子保存。"""

    def __init__(self, path: Path, backup_dir: Path, cfg: Config | None = None):
        self.path, self.backup_dir = Path(path), Path(backup_dir)
        self._lock = threading.RLock()
        self._warning = ""
        try:
            self._cfg = copy.deepcopy(cfg) if cfg is not None else load_config(self.path)
            validate_config(self._cfg)
        except ConfigError as error:
            if cfg is not None:
                raise
            self._cfg = Config()
            self._warning = str(error) + "。请重新填写并保存配置。"

    def snapshot(self) -> Config:
        with self._lock:
            return copy.deepcopy(self._cfg)

    def candidate(self, payload: dict) -> Config:
        with self._lock:
            return patch_config(self._cfg, payload)

    def public_state(self) -> dict:
        with self._lock:
            raw = {name: asdict(getattr(self._cfg, name)) for name in CONFIG_SECTIONS}
            raw["model"]["api_key_configured"] = bool(raw["model"].pop("api_key"))
            return {"config": raw, "config_warning": self._warning,
                    "env_overrides": [field for field, name in ENV_FIELDS.items()
                                      if os.environ.get(name, "").strip()]}

    def update(self, payload: dict) -> None:
        with self._lock:
            cfg = patch_config(self._cfg, payload)
            for field, name in ENV_FIELDS.items():
                value = os.environ.get(name, "").strip()
                expected = value.rstrip("/") if field == "base_url" else value
                if value and getattr(cfg.model, field) != expected:
                    raise ConfigError(f"{name} 正在覆盖此项，请先移除该环境变量再保存。")
            if self._warning and self.path.exists():
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                backup = self.backup_dir / ("config-backup-" + uuid.uuid4().hex + ".toml")
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(self.path.read_bytes())
            save_config(cfg, self.path)
            cfg.config_path = self.path
            cfg.missing_file = cfg.using_example = False
            # 文件替换成功后才切换运行配置。
            self._cfg, self._warning = cfg, ""
