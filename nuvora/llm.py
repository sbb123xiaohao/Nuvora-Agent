"""LLM 工厂、模型发现（/models）与诊断。"""

from __future__ import annotations

import httpx

from .config import WORKSPACE_DIR, Config


def redact_error(cfg: Config, error: object) -> str:
    text = str(error)
    return text.replace(cfg.model.api_key, "[已隐藏 API Key]") if cfg.model.api_key else text


def list_available_models(cfg: Config, timeout: float = 15.0) -> tuple[bool, list[str], str]:
    """请求 {base_url}/models 探测端点可用模型。

    返回 (是否成功, 模型 id 列表, 说明文字)。
    """
    base = cfg.model.base_url
    if not base:
        return False, [], "未配置 base_url"
    url = f"{base}/models"
    headers = {"Accept": "application/json"}
    if cfg.model.api_key:
        headers["Authorization"] = f"Bearer {cfg.model.api_key}"
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
    except (httpx.HTTPError, ValueError, ImportError) as e:
        return False, [], f"无法连接 {url}（{type(e).__name__}: {redact_error(cfg, e)}）"
    if resp.status_code in (401, 403):
        return False, [], (
            f"鉴权失败（HTTP {resp.status_code}）：API Key 缺失或无效。"
            f"端点本身可达，填好 key 后即可正常使用。"
        )
    if resp.status_code != 200:
        detail = redact_error(cfg, resp.text).replace("\n", " ")[:200]
        return False, [], (
            f"端点返回 HTTP {resp.status_code}（该端点可能不支持 /models 列表，"
            f"请手动填写 model 名）{detail}"
        )
    try:
        data = resp.json()
    except ValueError:
        return False, [], "响应不是合法 JSON"
    items = data.get("data") if isinstance(data, dict) else data
    if items is None and isinstance(data, dict):
        items = data.get("models")
    if not isinstance(items, list):
        return False, [], "端点返回的模型列表格式无效：data/models 应为数组"
    ids: list[str] = []
    for item in items or []:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
        elif isinstance(item, str):
            ids.append(item)
    ids = sorted(set(ids))
    if not ids:
        return False, [], "端点返回了空模型列表"
    return True, ids, f"共 {len(ids)} 个可用模型"


def build_chat_model(cfg: Config, model_override: str | None = None):
    """按配置构建一个指向任意 OpenAI 兼容端点的 ChatModel。"""
    from langchain_openai import ChatOpenAI

    name = (model_override or cfg.model.model).strip()
    if not name:
        raise ValueError(
            "未指定模型名：请运行 `python -m nuvora configure` 填写，"
            "或先运行 `python -m nuvora models` 探测可用模型"
        )
    return ChatOpenAI(
        model=name,
        api_key=cfg.model.api_key or "EMPTY",
        base_url=cfg.model.base_url,
        temperature=cfg.model.temperature,
        streaming=cfg.cli.stream,
        timeout=120,
        max_retries=2,
    )


def ping_model(cfg: Config) -> tuple[bool, str]:
    """用最小代价（1 token 上限）验证对话接口是否真正可用。"""
    try:
        llm = build_chat_model(cfg)
        reply = llm.invoke("ping", max_tokens=1)
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        return True, f"对话接口正常（回复片段：{text[:50]!r}）"
    except Exception as e:  # noqa: BLE001 —— 诊断场景需要看到任何错误
        return False, f"对话接口异常：{type(e).__name__}: {redact_error(cfg, e)}"


def run_doctor(cfg: Config, ping: bool = False) -> list[tuple[str, str, str]]:
    """逐项体检，返回 (检查项, 状态, 说明) 列表；状态 ∈ {ok, warn, fail}。"""
    import sys
    import tempfile

    checks: list[tuple[str, str, str]] = []

    version = sys.version.split()[0]
    checks.append(
        ("Python 版本", "ok" if sys.version_info >= (3, 11) else "fail", version)
    )

    if cfg.missing_file:
        checks.append(
            ("配置文件", "warn", "未找到 config.toml（当前用示例配置）——运行 python -m nuvora configure 保存设置")
        )
    elif cfg.using_example:
        checks.append(("配置文件", "warn", "正在使用 config.example.toml，运行 python -m nuvora configure 保存设置"))
    else:
        checks.append(("配置文件", "ok", str(cfg.config_path)))

    ok, ids, msg = list_available_models(cfg)
    checks.append(
        ("端点连通 (/models)", "ok" if ok else "warn", f"{cfg.model.base_url or '（空）'} → {msg}")
    )

    key = cfg.model.api_key
    masked = f"{key[:4]}****{key[-4:]}" if len(key) > 8 else ("已设置" if key else "")
    checks.append(
        (
            "API Key",
            "ok" if key else "fail",
            masked or "未设置——请在 config.toml 的 [model] api_key 填写（Ollama 可填 ollama）",
        )
    )

    if cfg.model.model:
        model_msg = cfg.model.model
        if ok and cfg.model.model not in ids:
            model_msg += "（注意：不在端点 /models 列表中，请确认拼写）"
        checks.append(("模型名", "ok", model_msg))
    else:
        checks.append(("模型名", "warn", "未指定——运行 `python -m nuvora models` 查看可用模型后填入"))

    try:
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=WORKSPACE_DIR, prefix=".doctor-", mode="w") as probe:
            probe.write("ok")
            probe.flush()
        checks.append(("工作区沙箱", "ok", str(WORKSPACE_DIR)))
    except OSError as e:
        checks.append(("工作区沙箱", "fail", f"不可写：{e}"))

    from .tools.python_repl import sandbox_status

    available, detail = sandbox_status(WORKSPACE_DIR)
    checks.append(("Python 执行隔离", "ok" if available else "warn", detail))

    try:
        import langchain  # noqa: F401
        import langchain_openai  # noqa: F401
        import langgraph  # noqa: F401
        from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: F401

        checks.append(("核心依赖", "ok", "langchain / langgraph / langchain-openai 可导入"))
    except Exception as e:  # noqa: BLE001
        checks.append(("核心依赖", "fail", str(e)))

    if ping:
        if not cfg.model.model:
            checks.append(("对话接口 (--ping)", "warn", "未指定模型名，跳过"))
        else:
            ok, msg = ping_model(cfg)
            checks.append(("对话接口 (--ping)", "ok" if ok else "fail", msg))

    return checks
