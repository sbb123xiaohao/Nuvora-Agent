# NUVORA — 通用 AI Agent 实施计划（WSL 测试版）

用户已确认：VPN 已开、网络可用；所有测试在 WSL 中进行（命令加 wsl 前缀）。

## 交付物
在 `C:\Users\sunaookamishiroko\Downloads\AI AGENT`（WSL 路径 `/mnt/c/Users/sunaookamishiroko/Downloads/AI AGENT`）下构建：

```
AI AGENT/
├── DESIGN.md              # 中文架构设计文档
├── README.md              # 快速上手
├── requirements.txt       # langgraph、langchain-openai、langgraph-checkpoint-sqlite、ddgs、rich、trafilatura 等
├── config.example.toml    # 配置模板（用户填 base_url/api_key/model）
├── smoke_test.py          # 无 API key 冒烟测试
├── workspace/             # 文件工具沙箱根目录
└── nuvora/
    ├── __init__.py        # 版本 + NUVORA 人格/系统提示词
    ├── config.py          # TOML 加载校验
    ├── llm.py             # LLM 工厂 + /v1/models 模型发现 + doctor 连通性检查
    ├── agent.py           # create_agent + SqliteSaver 会话持久化
    ├── memory.py          # SQLite 长期记忆 + remember/recall 工具
    ├── tools/             # web_search/web_fetch/文件沙箱/run_python/system
    └── cli.py             # rich 交互界面 + /new /model /tools 等斜杠命令
```

## 核心设计
1. **模型自定义**：config.toml 填任意 OpenAI 兼容端点（智谱 GLM/DeepSeek/OpenRouter/Ollama）；`nuvora models` 自动探测该端点可用模型列表；`nuvora doctor` 检查连通性。
2. **Agent 核心**：LangGraph 1.x `create_agent`（ReAct 循环）+ `SqliteSaver` 按 thread_id 持久化会话。
3. **记忆**：短期=checkpointer；长期=SQLite memories 表，remember/recall 工具 + 注入系统提示词。
4. **安全**：文件工具限制在 workspace/ 沙箱；run_python 子进程 + 超时；key 不落日志。

## 实施步骤（全部经 WSL 执行，VPN 网络可用）
1. WSL 环境检查：`wsl bash -c "python3 --version && pip3 --version"`（需 Python ≥3.10）
2. 编写全部文档与代码文件（Windows 侧 Write 工具直接写入项目目录）
3. WSL 内建 venv：`wsl bash -c "cd '/mnt/c/Users/sunaookamishiroko/Downloads/AI AGENT' && python3 -m venv .venv"`
4. 安装依赖：`wsl bash -c "... .venv/bin/pip install -r requirements.txt"`（VPN 直连 PyPI；失败则切清华镜像）
5. 测试（WSL 内）：
   - `wsl bash -c "... .venv/bin/python smoke_test.py"` —— 配置加载、工具沙箱、长期记忆、模型发现降级
   - `wsl bash -c "... .venv/bin/python -m nuvora doctor"` / `-m nuvora models`
   - 引导用户在 config.toml 填任一 API key 后进行真实对话验收

## 验收标准
- smoke_test.py 在 WSL 内全部通过（无需 key）
- doctor/models 命令行为正确
- 填 key 后可流式对话、调用工具、跨会话记忆