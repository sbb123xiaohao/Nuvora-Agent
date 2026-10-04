# NUVORA 架构设计文档

> **NUVORA** = **Nova**（新星）+ **Ora**（时刻）——「此刻升起的新星」
> 一个基于 LangGraph 的通用 AI 智能助理 · v0.1.0

---

## 1. 项目概述

### 1.1 定位

NUVORA 是一个运行在用户终端（WSL）里的**通用 AI 智能助理**：

| 能力 | 说明 |
|---|---|
| 对话 | 流式（逐消息）渲染，支持多会话、跨重启恢复 |
| 联网 | DuckDuckGo 搜索 + 网页正文提取 |
| 文件 | 在 `workspace/` 沙箱内读写文件、浏览目录 |
| 代码 | 执行 Python 片段（子进程 + 超时控制） |
| 记忆 | 会话检查点（短期）+ SQLite 长期记忆（跨会话） |
| 模型 | 任意 OpenAI 兼容端点，用户完全自定义，支持自动探测 |

### 1.2 设计原则

1. **模型无关**：NUVORA 不绑定任何一家模型厂商。任何提供 OpenAI 兼容接口的服务（智谱、DeepSeek、Moonshot、OpenRouter、Ollama、vLLM……）都能即插即用。
2. **少依赖、可读**：核心代码千行级，每个模块职责单一，适合阅读和二次开发。
3. **安全默认值**：文件沙箱、代码超时、key 不落日志，安全边界宁紧勿松。
4. **无 key 可运行**：除真实对话外，所有功能（配置、工具、记忆、自检）都可以在没有 API key 的情况下验证。

---

## 2. 调研依据（2026-10）

设计前对当前主流方案做了联网调研，结论：

| 调研点 | 结论 | 来源 |
|---|---|---|
| 框架选型 | LangGraph 以显式状态机架构成为生产级首选，长期维护的代码库中表现更好；OpenAI Agents SDK 更轻但绑定 OpenAI 生态 | [AI framework comparison 2026: LangChain vs LangGraph](https://www.yaitec.com)、[Agent Frameworks in 2026: Choosing the Right Runtime](https://suparious.com)、[Best AI Agent Frameworks in 2026](https://hirehal.ai)、[AI Agent Framework Comparison 2026](https://jangwook.net) |
| 核心循环 | ReAct（Thought → Action → Observation）+ 工具调用是当前最可靠的 agent 模式 | [Build a Simple ReAct Agent from Scratch](https://cognitiveclass.ai)、[ReAct Pattern](https://hopx.ai)、[I Built 100+ Gen AI Agents](https://dev.to) |
| 记忆 | 分层记忆是共识：工作记忆（上下文窗口）/ 情景记忆（会话日志）/ 语义记忆（长期知识库）；纯向量库不等于记忆 | [Memory Systems for AI Agents](https://agentcogito.com)、[Memory Is Not a Vector Database](https://dev.to)、[Long-term Memory in LLM Applications (LangMem)](https://langchain-ai.github.io) |
| 工具协议 | MCP（Model Context Protocol）是工具接入的事实标准，正向 agent-to-agent 演进；接入时需做安全边界（白名单、最小权限、审计） | [MCP Guide](https://www.buildmvpfast.com)、[MCP Security Best Practices: 2026 Checklist](https://www.swfte.com)、[MCP 2026 Roadmap](https://www.elegantsoftwaresolutions.com) |
| 模型接入 | OpenAI 兼容接口已成行业默认；智谱 GLM 等国产端点均支持 Function Calling 与 `/models` 风格列表 | [智谱开放平台文档](https://docs.bigmodel.cn)、[GLM API Guide](https://apidog.com) |

---

## 3. 总体架构

```
┌────────────────────────────────────────────────────────────┐
│                      CLI 终端（rich）                        │
│   对话输入 · 流式渲染 · /new /model /tools /memory 斜杠命令   │
└─────────────┬──────────────────────────────────────────────┘
              │ thread_id + 用户消息
              ▼
┌────────────────────────────────────────────────────────────┐
│              LangGraph Agent 核心循环（ReAct）               │
│                                                            │
│   ┌─────────┐   tool_calls    ┌──────────┐                 │
│   │ agent   │ ──────────────► │  tools   │                 │
│   │ (LLM)   │ ◄────────────── │  节点    │                 │
│   └─────────┘   observations  └──────────┘                 │
│        │                            │                      │
│        │  ┌──────────────────────┐  │                      │
│        └─►│ SqliteSaver 检查点    │◄─┘   （短期记忆：          │
│           │ data/checkpoints.db  │        每步持久化，        │
│           └──────────────────────┘        thread_id 隔离）  │
└──────┬──────────────────────────────┬──────────────────────┘
       ▼                              ▼
┌── 模型接入层 ──────────┐      ┌──── 工具系统 ────────────┐
│ config.toml（用户填）  │      │ web_search   联网搜索     │
│ base_url / api_key /  │      │ web_fetch    网页正文     │
│ model / temperature   │      │ list_dir/read_file/       │
│                       │      │   write_file  文件沙箱     │
│ ChatOpenAI ──► 任意   │      │ run_python    代码执行     │
│ OpenAI 兼容端点        │      │ current_time/system_info  │
│                       │      │ remember/recall/forget    │
│ GET /models 探测可用  │      │           长期记忆工具     │
│ 模型列表（nuvora      │      │                           │
│ models / doctor）     │      │ data/memory.db（长期记忆） │
└───────────────────────┘      └───────────────────────────┘
```

### 3.1 一次对话的完整数据流

1. 用户在 CLI 输入文本 → CLI 以 `("user", text)` 追加到消息列表，按当前 `thread_id` 调用 `agent.stream()`。
2. LangGraph 从 SqliteSaver 恢复该会话的历史状态，执行 **agent 节点**：把系统提示词 + 全部历史 + 本次消息发给模型。
3. 模型要么直接回答（循环结束），要么发起 `tool_calls` → **tools 节点**执行对应工具 → 观察结果作为 `ToolMessage` 回到 agent 节点 → 重复，直到模型给出最终回答或达到 `recursion_limit`。
4. 每一步的状态变更都写入 SqliteSaver；CLI 渲染过程消息（工具调用面板、结果面板）与最终回答（Markdown）。

---

## 4. 模型接入层

### 4.1 配置模型（三层优先级）

1. **环境变量**：`NUVORA_API_KEY` / `NUVORA_BASE_URL` / `NUVORA_MODEL`（最高）
2. **config.toml**（推荐，用户手填）
3. **内置默认值**（仅 base_url 有默认——智谱端点；key/model 必填否则无法对话）

### 4.2 模型发现协议

`nuvora models` / `/models` 向 `{base_url}/models` 发起 GET（带 Bearer 头），解析标准 OpenAI 格式 `{"data": [{"id": ...}]}`，同时兼容 `{"models": [...]}` 与纯字符串数组。失败时优雅降级：提示该端点可能不支持列表，引导手填模型名。

`nuvora doctor --ping` 用 `max_tokens=1` 的真实请求验证对话链路（key 有效性、模型名、端点路径一次性排查）。

### 4.3 为什么用 ChatOpenAI 而不是各家专用 SDK

OpenAI 兼容接口已是 2026 年的行业默认。`langchain_openai.ChatOpenAI(base_url=...)` 一套代码覆盖几乎所有主流与本地端点，避免为每个厂商维护适配器。专用 SDK（如 Anthropic 原生协议）作为后续扩展点。

---

## 5. 工具系统

### 5.1 工具清单

| 工具 | 功能 | 安全措施 |
|---|---|---|
| `web_search` | DuckDuckGo 搜索（ddgs，免 key） | 异常转字符串回给模型，不炸循环 |
| `web_fetch` | 抓网页 + trafilatura 提取正文（Markdown） | 仅 http/https；8KB 截断 |
| `list_dir` / `read_file` / `write_file` | 文件浏览/读取/写入 | **路径沙箱**（见 5.2）；读取 20K 字符截断 |
| `run_python` | 子进程执行 Python | 见 5.3 |
| `current_time` / `system_info` | 时间与环境信息 | 只读 |
| `remember` / `recall` / `forget` | 长期记忆增删查 | 仅操作 memory.db |

### 5.2 文件沙箱实现

`resolve_in_sandbox()` 是唯一的路径入口：

1. 统一斜杠，剥离绝对路径前缀与 Windows 盘符（模型偶尔会输出绝对路径）；
2. **拒绝任何含 `..` 的路径**；
3. `resolve()` 解析符号链接后，再校验目标仍在 `workspace/` 真实根内（防 symlink 逃逸）。

沙箱根 = 项目目录下的 `workspace/`，与代码目录隔离。

### 5.3 代码执行边界（诚实声明）

`run_python` 是**子进程级隔离**：独立解释器、`-I` 隔离模式（忽略用户环境变量与用户站点包）、工作目录限定 workspace/、超时强杀（默认 30s，上限 120s）、输出截断。**它不是容器级沙箱**——进程仍拥有当前用户的系统权限。原型场景够用；若要运行不受信代码，替换点在 `tools/python_repl.py`（换 Docker/nsjail/microVM 执行即可，接口不变）。

### 5.4 工具错误处理约定

所有工具**不抛异常**，把失败信息以字符串返回（`搜索失败：TimeoutError: ...`）。这让模型能读到失败原因并自行调整策略（换关键词、换路径、告知用户），符合 ReAct 的观察-再推理模式。

---

## 6. 记忆系统（三层）

| 层 | 存储 | 生命周期 | 实现 |
|---|---|---|---|
| 工作记忆 | 上下文窗口 | 单回合 | 由模型上下文长度决定；`recursion_limit` 防失控 |
| 情景记忆（会话） | `data/checkpoints.db`（SqliteSaver） | 跨重启，按 thread_id | LangGraph 检查点，每步自动持久化；`/sessions`、`/resume` 管理会话 |
| 语义记忆（长期） | `data/memory.db`（memories 表） | 永久 | 结构：`(id, created_at, content, tags, thread_id)`；关键词 LIKE 检索；每回合把最近 12 条注入系统提示词 |

**长期记忆的写入策略**：由模型自主判断——系统提示词指示「用户透露值得长期记住的信息（背景/偏好/项目/决定）时调用 remember」。这是当前主流的 agent 自主记忆模式；未采用向量检索的原因：原型阶段关键词检索足够、零额外依赖，升级路径明确（见 §10）。

---

## 7. 安全设计

| 威胁 | 缓解 |
|---|---|
| 路径逃逸读写任意文件 | 沙箱路径规范化 + `..` 拒绝 + resolve 后二次校验 |
| 代码执行失控/死循环 | 子进程超时强杀；代码长度上限；输出截断 |
| API key 泄漏 | key 仅在内存与 config.toml；doctor 输出打码（`sk-1****abcd`）；不写日志 |
| 提示词注入放大 | 工具输出以 ToolMessage 形式参与推理，敏感操作（删记忆需编号、写文件需明确意图）由系统提示约束 |
| 失控循环烧 token | `recursion_limit` 硬上限（默认 30 轮工具调用） |

生产加固建议（原型未做）：容器化执行 run_python；工具白名单按会话授权；MCP 接入时遵循「认证每个 server、最小权限、审计一切」清单。

---

## 8. 目录结构与模块职责

```
AI AGENT/
├── DESIGN.md / README.md      文档
├── requirements.txt           依赖（langgraph / langchain / langchain-openai /
│                              langgraph-checkpoint-sqlite / ddgs / trafilatura / rich / httpx）
├── config.example.toml        配置模板（复制为 config.toml 填 key）
├── smoke_test.py              10 项冒烟测试（无 key 可跑）
├── workspace/                 文件工具沙箱根
├── data/                      运行时生成：checkpoints.db + memory.db
└── nuvora/
    ├── __init__.py            名称/版本/系统提示词模板
    ├── config.py              TOML 加载 + 环境变量覆盖 + 校验
    ├── llm.py                 ChatOpenAI 工厂、/models 探测、doctor、ping
    ├── agent.py               create_agent 组装 + 系统提示词拼装（人格/环境/记忆块）
    ├── memory.py              LongTermMemory（SQLite）+ 记忆工具工厂
    ├── tools/
    │   ├── __init__.py        工具注册表：build_tools(cfg, memory) → list[BaseTool]
    │   ├── web.py             web_search / web_fetch
    │   ├── files.py           沙箱路径解析 + 文件三件套
    │   ├── python_repl.py     run_python_code
    │   └── system.py          时间 / 环境信息
    ├── cli.py                 ChatSession（流式渲染 + 斜杠命令）+ doctor/models 子命令
    └── __main__.py            python -m nuvora 入口
```

**配置速览**（完整注释见 `config.example.toml`）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `[model] base_url` | 智谱端点 | 任意 OpenAI 兼容地址 |
| `[model] api_key` / `model` | 空 | 必填才能对话 |
| `[model] temperature` | 0.7 | 采样温度 |
| `[agent] max_iterations` | 30 | 单回合工具调用轮数上限 |
| `[tools] python_timeout` | 30 | 代码执行超时（≤120） |
| `[memory] enabled` | true | 长期记忆开关 |
| `[cli] stream` | true | 逐消息流式渲染 |

---

## 9. 测试与验收

**冒烟测试**（`smoke_test.py`，10 项，无 API 消耗）：
配置加载 → 沙箱读写 → **4 种路径逃逸拦截** → 记忆增查删 → 记忆 LangChain 工具链 → run_python 执行/超时/异常三分支 → 不可达端点优雅降级 → Agent 图离线构建 → 系统提示词完整性 → 依赖完整性。

**真实对话验收**（需 key）：`doctor --ping` 通过 → CLI 对话 → 让它搜索并写文件 → 重启后 `/sessions` + `/resume` 恢复 → `记住…` 后新会话验证长期记忆生效。

---

## 10. 扩展路线

| 方向 | 说明 | 改动点 |
|---|---|---|
| Token 级流式 | `stream_mode="messages"` 逐字输出 | 仅 cli.py 渲染层 |
| 向量长期记忆 | 记忆检索换 embedding + SQLite-vss/Chroma | memory.py |
| MCP 接入 | 以 MCP client 身份接入外部工具 server | tools/ 新增 mcp.py |
| 子代理 | 复杂任务拆分给专职子 agent（LangGraph 多节点） | agent.py |
| Web UI | FastAPI + 前端，复用 ChatSession | 新增 server.py |
| 语音 | ASR/TTS 包一层 | cli.py |
| 原生协议适配 | Anthropic/Google 专用 SDK | llm.py 工厂加分支 |
