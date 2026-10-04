# NUVORA ✦ 此刻升起的新星

一个基于 **LangGraph** 的本地 AI 智能助理。**v0.4.0 默认使用 CLI**，在同一个终端完成对话、模型配置、工具开关、会话和长期记忆操作。

## 快速开始

安装 **Python 3.11+** 并解压完整项目：

- **Windows**：双击 `start.bat`。
- **Linux / WSL**：在项目目录运行 `./start.sh`。解压工具未保留执行权限时，先执行 `chmod +x start.sh`。
- 启动器首次创建 `.venv` 并安装已锁定的依赖，随后直接进入终端。首次安装需要网络。

第一次在交互终端启动且未配置模型时，会进入配置向导；以后可随时输入 `/setup` 重新配置。

1. 填写 OpenAI 兼容的接口地址和 API Key，密钥输入不回显。
2. 选择是否获取模型列表，然后填写模型名称。
3. 按需设置工具开关、长期记忆、温度、轮数和流式输出，确认保存后开始聊天。

模型回复需要支持工具调用的兼容接口。模型列表接口缺失时可手动填写名称；Ollama 需要自行安装、启动并下载模型，本地无鉴权端点可留空密钥。

设置原子保存到 `config.toml`，在下一回合生效。API Key 输入留空保留原值，输入 `-` 明确清除；保存前可以取消。非空环境变量 `NUVORA_BASE_URL`、`NUVORA_API_KEY`、`NUVORA_MODEL` 具有优先级，向导保留对应环境设置。

## 终端命令

| 命令 | 操作 |
|---|---|
| `/help` | 查看命令 |
| `/setup` 或 `/config` | 配置模型、密钥、工具和高级参数并保存 |
| `/new` | 开启新会话，保留旧历史 |
| `/sessions` | 查看历史会话编号 |
| `/resume <id>` | 恢复指定会话 |
| `/model <名称>` | 临时切换当前会话模型 |
| `/models` | 获取当前端点的模型列表 |
| `/tools` | 查看实际装配的工具 |
| `/memory [关键词]` | 查看或检索长期记忆 |
| `/quit` 或 `/exit` | 退出 |

直接输入文本即可对话，工具调用和结果会显示在终端中。按 Ctrl+C 中断当前回合，之后可以继续输入；在输入提示处按 Ctrl+C 或输入 `/quit` 退出。

联网、文件和 Python 开关影响 Agent 实际可用工具。长期记忆关闭后保留数据，但 Agent 不读取或写入这些记忆。终端与网页共用 SQLite 检查点，旧会话可继续恢复。

## 其他入口

已有 Python 环境可直接运行：

```bash
python -m pip install -r requirements.txt
python -m nuvora                         # 默认 CLI
python -m nuvora chat                    # 显式 CLI
python -m nuvora configure               # 独立终端配置向导
python -m nuvora doctor                  # 自检
python -m nuvora doctor --ping           # 极短的真实模型请求
python -m nuvora models                  # 模型列表
python -m nuvora version
```

启动器也支持相同子命令，例如 `./start.sh configure` 或 `start.bat doctor`。

若需要网页入口，显式运行 `python -m nuvora web`；可加 `--port 8766` 或 `--no-browser`。网页仅监听本机，支持配置、聊天、会话、记忆和文本工作区管理。

## Python 执行隔离

CLI 可在 Windows/Linux/WSL 使用。**Python 工具仅在 Linux/WSL，且系统隔离可用时执行**：需要 bubblewrap 0.9+、libseccomp，并允许隔离命名空间。使用系统包管理器安装这些组件，无需启动容器。

隔离不可用时工具拒绝执行，其余功能仍可使用；可在 `/setup` 关闭 Python 工具。执行不继承密钥、代理或用户环境，禁止联网，限制时间、资源和输出。已有资源限制不是整个工作区的磁盘配额。

停止会中断后续 Agent 步骤；已经开始的模型请求或工具可能先完成。中断恢复会保留已完成的结果，并补齐未完成工具的记录。

## 项目结构

| 文件/目录 | 用途 |
|---|---|
| start.bat / start.sh / start.py | 准备虚拟环境并进入 CLI，转发子命令 |
| nuvora/cli.py / terminal_setup.py | 终端对话、命令与配置向导 |
| nuvora/runtime.py / messages.py | 共享 Agent 执行、消息与中断恢复 |
| nuvora/config.py | 配置快照、草稿、校验和原子保存 |
| nuvora/sessions.py / memory.py | 会话仓库与长期记忆 |
| nuvora/agent.py / llm.py | 图装配、模型工厂和端点诊断 |
| nuvora/application.py | 应用协调、回合占用和资源生命周期 |
| nuvora/web_ui.py / static/ | 可选本地网页与 HTTP/SSE 入口 |
| nuvora/tools/ | 联网、工作区文件、Python 和系统工具 |
| config.example.toml | 配置字段模板；向导可直接生成 config.toml |
| requirements.txt / requirements.lock.txt | 已验证的固定依赖，不指定镜像源 |
| data/ | 运行时 SQLite 会话与记忆，不进入 Git |
| workspace/ | Agent 工作区，生成文件不进入 Git |
| tests/ / smoke_test.py | 不需要真实 API Key 的回归与冒烟检查 |

模块边界和扩展方式见 [DESIGN.md](DESIGN.md)。

## 验证

```bash
python -m unittest discover -s tests -v
python smoke_test.py
python -m pip check
```

回归覆盖终端配置保存/取消、密钥不回显与环境变量优先级，以及真实 Agent 工具循环、SQLite 重开、历史恢复、并发、文件边界、断流和资源清理。可选网页使用真实 HTTP 与本地 OpenAI 兼容模拟端点验证发现、连接、流式和停止后继续；模拟端点不代表真实云端提供商验收。

系统禁止隔离时，实际 Python 隔离测试会明确跳过，工具仍拒绝回退到无隔离执行。
