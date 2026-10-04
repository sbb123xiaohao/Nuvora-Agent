# NUVORA ✦ 此刻升起的新星

一个基于 **LangGraph** 的通用 AI 智能助理，运行在你的终端里（WSL）。

当前版本 **0.1.1**，需要 **Python 3.11+**。Linux/WSL 的 Python 执行还需要
**bubblewrap 0.9+、libseccomp** 及系统允许的隔离命名空间。请通过系统包管理器安装这两个组件。
如果系统不允许隔离，`run_python` 会返回不可用原因；其余对话、联网、文件与记忆功能仍可使用。

- 🌐 **模型自定义**：任意 OpenAI 兼容端点（智谱 GLM / DeepSeek / OpenRouter / Ollama 本地模型……），填 `base_url` + `api_key` 即用
- 🔍 **自动探测模型**：`nuvora models` 一键列出端点下的所有可用模型
- 🛠 **内置 8+3 工具**：联网搜索、网页阅读、文件读写（沙箱）、Python 代码执行、时间/系统信息 + 长期记忆（remember / recall / forget）
- 🧠 **双层记忆**：会话检查点（可跨重启恢复）+ 长期记忆（串行事务保护并发写入）
- ⚡ **ReAct 循环**：推理 → 调工具 → 观察 → 再推理，直到给出答案

---

## 快速开始

### 1. 安装依赖（已装好可跳过）

`requirements.txt` 和 `requirements.lock.txt` 固定了通过执行验证的依赖组合。
已有环境也应重新运行安装命令，升级旧的 SQLite 检查点组件；HTTPX 的 SOCKS 支持已包含在依赖里。

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
├── requirements.txt     # 直接依赖及版本约束
├── requirements.lock.txt # 完整依赖锁定，不指定镜像源
├── config.example.toml  # 配置模板 → 复制为 config.toml
├── smoke_test.py        # 无 key 冒烟测试
├── tests/               # 无 API 回归，含真实 Agent/SQLite 执行
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
- **Python 执行不可用** → `doctor` 会报告原因。安装 bubblewrap/libseccomp，并确认系统允许用户、进程、网络和挂载命名空间；原生 Windows 请在 WSL 中运行。
- **文件权限** → 文件工具限制在 `workspace/`。Python 使用操作系统隔离：工作区可写、Python 运行时只读、宿主私有目录不挂载、禁止联网、只传递固定环境变量。
- **执行限额** → Python 默认 30 秒、最长 120 秒；每个进程内存上限 512 MiB，单文件写入上限 32 MiB，临时目录 64 MiB；管道输出在收集时限长。超时会清理执行进程及其后代。这些限额不等于工作区总磁盘配额。
- **本地数据** → `config.toml`、`data/`、工作区生成文件和虚拟环境均排除在 Git 提交之外。

## 验证

```bash
.venv/bin/python smoke_test.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

测试不调用真实模型 API。冒烟测试现在会执行工具循环并写入 SQLite，回归还会重新打开数据库验证历史上下文。
隔离受限的环境会明确跳过实际 Python 沙箱测试，同时继续验证“隔离缺失时拒绝执行”、环境清理、输出限长和后代进程超时清理。
跳过不代表对应功能已验收；应在目标 Linux/WSL 环境运行 `doctor` 和这些测试。

更完整的架构说明、安全边界与扩展路线见 [DESIGN.md](DESIGN.md)。
