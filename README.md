# NUVORA ✦ 此刻升起的新星

一个基于 **LangGraph** 的本地 AI 智能助理。**v0.3.0 默认打开统一网页界面**：聊天、模型配置、工具开关、会话、长期记忆和工作区都在同一个页面中操作。

## 快速开始

先安装 **Python 3.11+**，然后解压完整项目。

- **Windows**：双击 `start.bat`。
- **Linux / WSL**：在项目目录运行 `./start.sh`；如果解压工具未保留执行权限，先执行 `chmod +x start.sh`。
- 启动器首次自动建立 `.venv` 并安装已锁定的依赖，之后直接打开浏览器。首次安装需要网络；保持启动窗口运行。

无需手动编辑配置文件。在页面右侧完成这几步即可：

1. 选择服务提供商，或填写一个 OpenAI 兼容的接口地址。
2. 输入 API Key，点击“获取模型”后选择模型，也可以手动填写名称。
3. 点击“保存设置”，开始聊天；“测试连接”会发起一次极短的实际模型请求。

默认只监听本机 `http://127.0.0.1:8765`，不会对外发布服务。模型回复需使用支持工具调用的 OpenAI 兼容接口；模型列表接口缺失时可手动填写模型名称。Ollama 需要用户自行安装、启动并下载所需模型。

## 一个界面完成操作

| 位置 | 操作 |
|---|---|
| 左侧 | 新建与恢复会话，查看长期记忆、工作区 |
| 中间 | 流式聊天、查看工具过程、复制回答、停止生成 |
| 右侧 | 服务提供商、接口地址、密钥、模型选择、连接测试、保存设置 |
| 工具与记忆 | 允许或禁用 Agent 的联网、文件操作、Python 和长期记忆 |
| 高级设置 | 温度、工具轮数、Python 超时、搜索条数、流式输出 |
| 长期记忆 | 添加、搜索和删除用户偏好与项目信息 |
| 工作区 | 查看目录、读取与编辑文本文件，路径仅限 workspace |

设置保存到 `config.toml`，下一次对话立即使用。API Key 不通过配置读取接口返回；密钥输入留空会保留原值，点击“清除密钥”并保存才会清除。环境变量仍具有优先级，页面会显示被环境变量覆盖的配置项。

开启/关闭工具会改变 Agent 实际可用的工具。关闭长期记忆后，保留已有内容供用户管理，但 Agent 不会读取或写入这些记忆。已有 CLI 会话保存在同一个 SQLite 检查点文件中，网页界面可以直接恢复。

停止生成会取消后续 Agent 步骤；已经开始的模型请求或工具可能需要先完成。界面会保存已完成的工具结果，避免下一次对话留下不完整的工具调用记录。

## Python 执行隔离

聊天、模型设置、联网和文件操作可在 Windows/Linux/WSL 使用。**Python 工具只在 Linux/WSL 中，且系统隔离可用时运行**：需要 bubblewrap 0.9+、libseccomp，并允许隔离命名空间。使用系统包管理器安装这些组件；无需启动容器。

隔离不可用时，页面会显示状态，Python 工具会拒绝执行；其余功能可继续使用。可在右侧关闭 Python 工具。Python 执行不继承密钥、代理或用户环境，禁止联网，限制时间、资源和输出；已有资源限制不是工作区的总磁盘配额。

## 已有 Python 环境

~~~bash
python -m pip install -r requirements.txt
python -m nuvora
~~~

不带命令默认启动网页界面。其他入口：

~~~bash
python -m nuvora web --port 8766        # 自定义本地端口
python -m nuvora web --no-browser       # 不自动打开浏览器
python -m nuvora chat                   # 保留原有交互终端
python -m nuvora doctor                 # 终端自检
python -m nuvora doctor --ping          # 一次真实模型请求
python -m nuvora models                 # 终端模型列表
python -m nuvora version
~~~

终端仍支持 `/new`、`/sessions`、`/resume`、`/model`、`/tools`、`/memory` 和 `/quit`。配置也可通过环境变量 `NUVORA_BASE_URL`、`NUVORA_API_KEY`、`NUVORA_MODEL` 覆盖。

## 项目结构

| 文件/目录 | 用途 |
|---|---|
| start.bat / start.sh / start.py | 首次准备虚拟环境并打开界面 |
| nuvora/web_ui.py | 本地 HTTP、来源与 CSRF 校验、静态资源和 SSE |
| nuvora/application.py | 应用操作、单回合占用、取消和资源生命周期 |
| nuvora/config.py | 配置快照、草稿校验、脱敏视图和原子保存 |
| nuvora/sessions.py | 会话标题、检查点访问和旧会话兼容 |
| nuvora/runtime.py / messages.py | 网页与终端共用的 Agent 执行、消息和中断恢复 |
| nuvora/static/ | 本地 HTML/CSS/JavaScript，无 CDN 依赖 |
| nuvora/agent.py / memory.py | Agent 图与长期记忆 |
| nuvora/tools/ | 联网、文件、Python、时间和环境工具 |
| nuvora/cli.py | 原有终端入口 |
| config.example.toml | 配置模板；页面会直接生成 config.toml |
| requirements.txt / requirements.lock.txt | 已验证的固定依赖，不指定镜像源 |
| data/ | 自动生成的 SQLite 会话与记忆；不进入 Git |
| workspace/ | Agent 的工作区；用户生成文件不进入 Git |
| tests/ / smoke_test.py | 无需真实 API Key 的回归与冒烟检查 |

## 验证

~~~bash
python -m unittest discover -s tests -v
python smoke_test.py
python -m pip check
~~~

回归包含真实 Agent 工具循环、SQLite 重开、并发写入、HTTP 配置/会话/记忆/文件操作、来源与 CSRF 校验，以及本地模拟 OpenAI 兼容端点的模型发现、连接测试、流式输出和停止后续聊。模拟端点仅存在于测试中；产品始终调用用户配置的实际模型。

架构回归还验证配置快照不会被调用方修改、保存失败不改变下一回合配置、并发输入只占用一个回合、断流与清理失败释放占用、关闭应用延迟到回合退出、启动失败关闭已打开的数据库，以及历史工具不会干扰最新回复的中断恢复。模块边界和扩展方式见 [DESIGN.md](DESIGN.md)。

系统禁止隔离时，实际 Python 隔离测试会明确跳过；工具仍拒绝回退到无隔离执行。
