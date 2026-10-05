# NUVORA Agent

在终端里对话、配置模型、调用工具和继续历史任务。

NUVORA 是基于 LangGraph 的本地 AI 助理，使用 OpenAI Chat Completions 兼容接口。**v0.5.0 为纯 CLI 版本**：启动即进入终端，模型设置、会话、文件和记忆都通过命令完成。

- **终端对话**：流式 Markdown 回复，显示工具调用与结果，支持 Ctrl+C 中断当前回合。
- **模型配置**：`/setup` 保存接口、密钥、模型和工具开关；`/model` 临时切换当前会话的模型。
- **任务工具**：联网搜索、正文提取、工作区文件读写、隔离 Python 执行和系统信息。
- **本地持久化**：SQLite 保存会话上下文与长期记忆，退出、重启后可继续。

## 快速开始

需要 **Python 3.11 或更新版本**，以及可用的模型端点。解压完整项目包，进入项目目录后启动：

| 系统 | 启动命令 |
| --- | --- |
| Windows | 双击 `start.bat`，或在终端运行 `.\start.bat` |
| Linux / macOS | `bash start.sh` |
| 已有 Python 环境 | `python start.py` |

启动脚本会创建项目内的 `.venv` 并安装锁定的依赖。首次启动需要联网下载依赖；以后仅在依赖清单变化时重新安装。

首次进入交互终端且尚未设置模型时，会自动打开配置向导。按提示填写：

1. **接口地址**：使用服务商提供的基础地址，例如 `https://your-endpoint.example/v1`；不要填写完整的 `/chat/completions` 路径。
2. **API Key**：输入不回显；留空保留已存的密钥，输入 `-` 清除。
3. **模型名称**：可选择探测 `/models`；端点不支持列表时直接填写名称。
4. 确认保存，随后直接输入任务。

```text
你 › 帮我整理一份学习计划，保存到 notes/plan.md
你 › /files notes
你 › /read notes/plan.md
你 › /new
你 › /sessions
你 › /resume <会话编号>
你 › /history
```

## 终端命令

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看所有命令 |
| `/setup` | 配置模型、工具和高级参数，保存后下一回合生效 |
| `/status` | 查看模型、接口、会话和配置路径；密钥只显示设置状态 |
| `/new` | 创建新会话，保留原会话 |
| `/sessions` | 列出会话编号、标题和当前会话标记 |
| `/resume <id>` | 继续指定会话，支持旧版本历史和新建空会话 |
| `/history` | 显示当前会话记录；工具结果显示摘要 |
| `/models` | 探测当前端点的可用模型 |
| `/model <名称>` | 临时切换当前会话模型；省略名称可查看当前值 |
| `/tools` | 列出当前可装配的工具 |
| `/files [目录]` | 浏览 `workspace/`，省略目录时查看根目录 |
| `/read <路径>` | 查看工作区文本文件，最多显示 20,000 字符 |
| `/memory [关键词]` | 查看或搜索长期记忆 |
| `/remember <内容>` | 直接保存一条记忆，无需调用模型 |
| `/forget <编号>` | 删除一条记忆，编号可从 `/memory` 获取 |
| `/quit` | 退出，也支持 `/exit`、`/q` |

回答过程中按 **Ctrl+C** 停止当前回合，随后可继续输入。已开始的网络请求或工具可能先完成，中断不会撤销已写入的文件。输入提示符处按 Ctrl+C 或发送 EOF 会退出。

## 子命令与手动安装

启动脚本可以转发子命令，例如 `python start.py configure`、`bash start.sh doctor --ping`。如果自行管理环境，在项目目录安装：

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell 改用：.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m nuvora
```

| 子命令 | 作用 |
| --- | --- |
| `python -m nuvora` / `python -m nuvora chat` | 进入交互终端 |
| `python -m nuvora configure` | 单独打开配置向导 |
| `python -m nuvora models` | 探测模型列表 |
| `python -m nuvora doctor` | 检查配置、依赖、工作区、隔离能力和模型列表接口 |
| `python -m nuvora doctor --ping` | 额外发起一次模型对话验证，可能产生 API 费用 |
| `python -m nuvora version` | 查看版本 |
| `python -m nuvora --help` | 查看用法 |

## 配置

`/setup` 会生成项目根目录的 `config.toml`。也可复制 [config.example.toml](config.example.toml) 后手动编辑。完整字段见示例文件：

```toml
[model]
base_url = "https://your-endpoint.example/v1"
api_key = "your-api-key"
model = "your-model-id"
temperature = 0.7

[tools]
web_enabled = true
files_enabled = true
python_enabled = true

[memory]
enabled = true

[cli]
stream = true
```

优先级为：**非空环境变量 → `config.toml` → 示例配置与内置默认值**。

| 环境变量 | 对应字段 |
| --- | --- |
| `NUVORA_BASE_URL` | `model.base_url` |
| `NUVORA_API_KEY` | `model.api_key` |
| `NUVORA_MODEL` | `model.model` |

向导会提示被环境变量覆盖的字段；修改这些字段前，先移除对应环境变量。配置按草稿校验后原子保存，取消或保存失败时继续使用原配置。`/model` 的临时选择随会话隔离；`/setup` 保存成功会清除当前会话的临时模型选择。

`stream = true` 时，交互终端逐 token 更新 Markdown；重定向输出时显示完整消息。设为 `false` 则等待整个 Agent 回合结束再展示结果。

## 数据与工具边界

| 位置 | 内容 |
| --- | --- |
| `config.toml` | 本地配置，可能包含 API Key |
| `data/checkpoints.db` | LangGraph 会话上下文 |
| `data/interface.db` | 会话标题，保留旧版本文件名以兼容已有数据 |
| `data/memory.db` | 长期记忆 |
| `workspace/` | Agent 可以读写的文件 |

以上运行数据已由 Git 忽略。升级时保留自己的 `config.toml`、`data/` 和 `workspace/`，替换源码后重新启动；备份运行数据前先退出 NUVORA。旧网页版本的会话和记忆可以继续通过 CLI 使用。

模型请求会把当前任务、对话上下文和启用的记忆发送到所配置的端点。联网工具会访问外部服务，文件工具限制在 `workspace/` 内；`/files`、`/read` 同样使用受限路径解析。

Python 执行需要 **Linux 或 WSL** 下可用的 `bubblewrap` 和 `prlimit`。原生 Windows、macOS 或禁止 namespace 的环境可以使用其余 CLI 功能，Python 工具在隔离不可用时拒绝执行。可通过 `/setup` 关闭它，使用 `doctor` 查看原因。

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| 未配置模型 | 输入 `/setup`，或用 `/models` 探测后 `/model <名称>` 临时启用 |
| 401 / 403 | 检查 API Key、服务端权限和环境变量覆盖 |
| 404 / 模型不存在 | 核对基础地址和模型名；不支持 `/models` 时可手动填写 |
| 连接超时 | 检查网络、代理和接口地址，运行 `doctor --ping` |
| 配置格式错误 | 运行 `python start.py configure` 重新填写；修复保存前会备份原文件到 `data/` |
| 历史没有自动显示 | 用 `/sessions` 选择会话，再输入 `/history` |
| Python 隔离不可用 | 查看 `doctor` 的原因，修复系统隔离条件或关闭 Python 工具 |
| 向导提示需要交互终端 | 从终端启动 `configure`；无交互环境可编辑配置或使用环境变量 |

## 开发与验证

模块职责及中断恢复方式见 [DESIGN.md](DESIGN.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 命令分发、对话循环和资源生命周期 |
| `terminal_setup.py` / `terminal_output.py` | 配置输入、流式 Markdown 和工具展示 |
| `config.py` | 快照、校验、环境变量覆盖、原子保存 |
| `sessions.py` / `memory.py` | 会话和长期记忆 |
| `runtime.py` / `agent.py` | 图执行、工具循环和中断恢复 |
| `llm.py` / `tools/` | 模型适配、端点诊断和能力注册 |

```bash
python -m unittest discover -s tests -v
python smoke_test.py
python -m pip check
```

测试使用离线模型执行真实 Agent 图，并用本地兼容端点验证发现、鉴权和流式/非流式对话，无需真实云端密钥。覆盖配置保存、会话恢复、中断、数据库清理、并发记忆和文件路径边界；系统隔离不可用时，实际隔离测试会明确跳过。
