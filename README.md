# NUVORA ✦ 此刻升起的新星

一个基于 **LangGraph** 的通用 AI 智能助理，运行在你的终端里（WSL）。

- 🌐 **模型自定义**：任意 OpenAI 兼容端点（智谱 GLM / DeepSeek / OpenRouter / Ollama 本地模型……），填 `base_url` + `api_key` 即用
- 🔍 **自动探测模型**：`nuvora models` 一键列出端点下的所有可用模型
- 🛠 **内置 8+3 工具**：联网搜索、网页阅读、文件读写（沙箱）、Python 代码执行、时间/系统信息 + 长期记忆（remember / recall / forget）
- 🧠 **双层记忆**：会话检查点（重启不丢对话）+ 长期记忆（记住你的偏好与项目）
- ⚡ **ReAct 循环**：推理 → 调工具 → 观察 → 再推理，直到给出答案

---

## 快速开始

### 1. 安装依赖（已装好可跳过）

```bash
wsl bash -c "cd '/mnt/c/Users/sunaookamishiroko/Downloads/AI AGENT' && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
```

### 2. 配置模型

复制配置模板并填写：

```bash
wsl bash -c "cd '/mnt/c/Users/sunaookamishiroko/Downloads/AI AGENT' && cp config.example.toml config.toml && nano config.toml"
```

需要填两个字段：

```toml
[model]
base_url = "https://open.bigmodel.cn/api/paas/v4"   # 任意 OpenAI 兼容端点
api_key  = "你的key"
model    = "glm-4.6"                                 # 模型名
```

> 不知道填什么模型名？先跑第 3 步的 `models` 命令探测，或者直接看下面的常用组合。

### 3. 自检 & 探测模型

```bash
cd '/mnt/c/Users/sunaookamishiroko/Downloads/AI AGENT'

# 环境自检（检查配置、连通性、key、依赖）
wsl bash -c "cd '/mnt/c/Users/sunaookamishiroko/Downloads/AI AGENT' && .venv/bin/python -m nuvora doctor"

# 探测当前端点有哪些可用模型
wsl bash -c "cd '/mnt/c/Users/sunaookamishiroko/Downloads/AI AGENT' && .venv/bin/python -m nuvora models"

# 附加一次真实对话验证（消耗极少 token）
wsl bash -c "cd '/mnt/c/Users/sunaookamishiroko/Downloads/AI AGENT' && .venv/bin/python -m nuvora doctor --ping"
```

### 4. 开聊！

```bash
wsl bash -c "cd '/mnt/c/Users/sunaookamishiroko/Downloads/AI AGENT' && .venv/bin/python -m nuvora"
```

试一句话让它跑起来：

```
你 › 搜一下今天的 AI 新闻，挑三条最重要的写进 workspace/news.md
你 › 帮我用 Python 算一下 2 的 100 次方有多少位
你 › 记住：我的服务器 IP 是 192.168.1.8        （存入长期记忆）
```

---

## 常用模型配置组合

| 厂商 | base_url | model 示例 | 说明 |
|---|---|---|---|
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.6`、`glm-4.5-flash` | flash 系列便宜/免费额度 |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` | — |
| Moonshot | `https://api.moonshot.cn/v1` | `kimi-k2` | — |
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-4` 等 | 一个 key 用百款模型 |
| Ollama 本地 | `http://localhost:11434/v1` | `qwen3:8b` | api_key 填 `ollama`，完全离线 |

---

## 会话内命令

| 命令 | 作用 |
|---|---|
| `/help` | 显示帮助 |
| `/new` | 开新会话（旧的保留） |
| `/sessions` | 列出历史会话 |
| `/resume <id>` | 回到某个历史会话 |
| `/model <名>` | 临时切换模型 |
| `/models` | 探测端点可用模型 |
| `/tools` | 查看已装配工具 |
| `/memory [词]` | 查看/检索长期记忆 |
| `/quit` | 退出 |

---

## 项目结构

```
AI AGENT/
├── DESIGN.md            # 架构设计文档（先看这个）
├── README.md            # 本文件
├── requirements.txt     # 依赖清单
├── config.example.toml  # 配置模板 → 复制为 config.toml
├── smoke_test.py        # 无 key 冒烟测试
├── workspace/           # Agent 的文件沙箱（它只能碰这里）
├── data/                # 运行时生成：会话检查点 + 长期记忆（SQLite）
└── nuvora/              # 源码
    ├── config.py        # 配置加载
    ├── llm.py           # 模型工厂 + /models 探测 + doctor
    ├── agent.py         # LangGraph Agent 组装
    ├── memory.py        # 长期记忆
    ├── tools/           # 工具系统
    └── cli.py           # 交互终端
```

## 常见问题

- **401 Unauthorized** → `api_key` 不对或没填。
- **404 Not Found** → 模型名拼写错误，或 `base_url` 少了 `/v1` 之类的前缀；用 `nuvora models` 核对。
- **连不上端点** → 检查网络/VPN；Ollama 需先 `ollama serve`。
- **想重置对话记忆** → 删除 `data/` 目录下对应文件（`checkpoints.db` = 会话，`memory.db` = 长期记忆）。
- **安全须知** → Agent 只能读写 `workspace/`；`run_python` 是子进程级隔离（有超时），不要让它执行来源不明的危险代码。

更完整的架构说明、安全边界与扩展路线见 [DESIGN.md](DESIGN.md)。
