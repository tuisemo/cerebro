# Cerebro — 群体智能协同引擎

> 基于 Discord 的 Factory Droid CLI 协同调度机器人，支持多场景任务模式、智能工作区隔离与流式交互。
>
> 📖 [English Version](README_EN.md)

## 项目概览

Cerebro 是一个 Discord Bot，通过调用 Factory Droid CLI 的 headless 模式，实现基于 Discord 的远程代码仓库协同开发。支持多用户并发、工作区隔离、智能任务解析和流式状态反馈。

### 核心特性

- **多场景任务模式**：支持 repo 克隆、指定工作区、临时目录、纯问答四种模式
- **智能工作区隔离**：每个 Discord Thread 拥有独立工作区，防止 Git 冲突
- **并发任务队列**：支持配置并发上限，超额任务自动排队
- **流式状态面板**：实时更新任务状态，避免刷屏
- **人机协同审批**：高危命令自动拦截，等待用户确认
- **任务持久化**：SQLite 存储任务状态，支持崩溃恢复和继续对话

---

## 快速开始

### 环境要求

- Python >= 3.12
- Windows（已适配原生环境）
- Git（需配置 `core.longpaths true`）

### 安装依赖

```bash
# 使用 uv 安装（推荐）
uv sync

# 或使用 pip
pip install -e .
```

### 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```ini
# Discord Bot Token（必填）
DISCORD_BOT_TOKEN=your_bot_token_here

# Factory API Key（可选，如已配置在系统环境变量）
FACTORY_API_KEY=fk-your-api-key

# 默认模型（可选）
DEFAULT_MODEL=custom:MiniMax-M2.7

# 工作区目录（可选，默认 ./droid_workspaces）
WORKSPACES_DIR=./droid_workspaces

# 并发任务数（可选，默认 2）
MAX_CONCURRENT_TASKS=2
```

### Discord Bot 配置

1. 访问 [Discord Developer Portal](https://discord.com/developers/applications) 创建 Application
2. 进入 **Bot** 页面，开启 `Message Content Intent`
3. 获取 **BOT_TOKEN**，填入 `.env`
4. 进入 **OAuth2 → URL Generator**：
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Manage Threads`, `Read Message History`, `Attach Files`
5. 复制生成的 URL 邀请 Bot 加入服务器

### 启动 Bot

```bash
# 方式1: 使用 uv
uv run cerebro

# 方式2: 使用 Python 模块
python -m cerebro

# 方式3: 直接运行
python cerebro/app.py
```

---

## 使用指南

### 基础命令

```
/task <描述> [repo:<路径>] [workspace:<路径>]
```

#### 场景示例

| 场景 | 命令 |
|------|------|
| 纯问答 | `/task 解释什么是装饰器` |
| Repo 模式 | `/task 重构 login 模块 repo:D:/Projects/MyApp` |
| 指定工作区 | `/task 写个备份脚本 workspace:D:/scripts` |
| 临时目录 | `/task 创建一个 Flask 应用` |

### 可用斜杠命令

| 命令 | 描述 |
|------|------|
| `/task <描述> [repo/workspace]` | 提交任务到队列 |
| `/status` | 查看系统状态（活跃任务、队列长度） |
| `/new` | 在当前 Thread 开启新会话，清理工作区 |
| `/cleanup <thread_id>` | 手动清理指定任务资源 |

### 支持的模型

- MiniMax-M2.7 (默认)
- Claude 4.6 / Sonnet 4.6
- GPT-5.4 Mini
- Gemini 3 Flash
- Qwen 3.5 Plus

---

## 项目结构

```
cerebro/
├── __init__.py      # 版本信息
├── __main__.py      # 模块入口 (python -m cerebro)
├── app.py           # Bot 主入口、命令定义、消息拦截
├── runner.py        # Droid CLI 进程控制与事件流解析
├── handler.py       # Droid 事件处理器，映射到 Discord 交互
├── workspace.py     # 工作区管理器（Git 克隆、沙盒隔离）
├── registry.py      # SQLite 任务持久化与状态管理
├── parser.py        # 任务指令解析器（repo/workspace 参数提取）
├── ui.py            # Discord 状态面板（TaskDashboard）
└── throttle.py      # 消息速率限制器
```

### 核心模块说明

#### `app.py` — Bot 主入口

- Discord Bot 实例初始化
- 斜杠命令定义（`/task`, `/status`, `/new`, `/cleanup`）
- 后台 Worker 任务队列消费
- 消息拦截与多模态附件处理
- 任务生命周期管理

#### `runner.py` — Droid 进程控制

- 动态查找 `droid` 可执行文件
- 进程启动与参数构建
- JSON 事件流解析
- Windows 环境适配（编码容错、Shell 提示注入）

#### `workspace.py` — 工作区管理

支持三种工作区模式：
- **Repo 模式**：`git clone --local` 克隆指定仓库
- **Workspace 模式**：直接使用指定目录
- **Temp 模式**：创建临时 Git 仓库

自动生成代码变更 Patch，支持自动清理过期工作区。

#### `handler.py` — 事件处理器

将 Droid CLI 的 JSON 事件流映射为 Discord 交互：
- `assistant_chunk` → 消息缓冲与分段发送
- `tool_call` → 工具执行状态通知
- 智能风险分级：高危命令阻塞确认、中等风险自动通知

#### `parser.py` — 指令解析

解析 `/task` 命令参数：
```python
parse_task_command("重构 login repo:D:/MyApp")
# → ParsedTask(task="重构 login", repo="D:/MyApp", is_file_operation=True)
```

自动检测文件操作关键词，决定是否需要创建工作区。

#### `registry.py` — 任务持久化

SQLite 存储：
- 任务元数据（Thread ID、工作区路径、模型、类型）
- 状态跟踪（active → completed → closed）
- 支持任务恢复（已完成任务可继续对话）
- 自动清理过期工作区

---

## 技术架构

### 数据流

```
Discord User
     ↓
[ /task command ]
     ↓
parser.py (解析参数)
     ↓
asyncio.Queue (任务队列)
     ↓
task_worker (并发 Worker)
     ↓
workspace.py (获取/创建工作区)
     ↓
runner.py (启动 Droid 进程)
     ↓
handler.py (事件流处理)
     ↓
Discord Thread (消息/Embed 更新)
```

### 并发控制

```python
MAX_CONCURRENT_TASKS = 2  # 可配置
droid_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
```

超额任务自动排队，Worker 循环消费队列。

### 工作区隔离

每个 Thread ID 对应独立工作区：
```
./droid_workspaces/
├── 123456789/          # Thread ID
│   ├── .git/
│   └── ...
└── 987654321/
    └── ...
```

### 风险分级机制

| 级别 | 命令模式 | 处理方式 |
|------|----------|----------|
| 高危 | `rm -rf`, `del /`, `format` 等 | Discord 按钮阻塞确认 |
| 中等 | `Execute`, `execute_command` | 发送通知，3秒自动继续 |
| 普通 | 其他工具 | 直接放行 |

---

## 配置选项

### 环境变量

| 变量 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `DISCORD_BOT_TOKEN` | ✅ | - | Discord Bot Token |
| `FACTORY_API_KEY` | ❌ | - | Factory API Key |
| `DEFAULT_MODEL` | ❌ | `custom:MiniMax-M2.7` | 默认 AI 模型 |
| `WORKSPACES_DIR` | ❌ | `./droid_workspaces` | 工作区根目录 |
| `MAX_CONCURRENT_TASKS` | ❌ | `2` | 并发任务上限 |

### pyproject.toml

```toml
[project]
name = "cerebro"
version = "2.0.0"
description = "Cerebro — 群体智能协同引擎"
requires-python = ">=3.12"
dependencies = [
    "discord.py>=2.3.0",
    "python-dotenv>=1.0.0",
]

[project.scripts]
cerebro = "cerebro.app:main"
```

---

## 开发指南

### 本地开发

```bash
# 安装开发依赖
uv pip install -e "."

# 运行测试（如有）
pytest

# 代码格式化
ruff format cerebro/

# 类型检查
mypy cerebro/
```

### 扩展新命令

在 `app.py` 中添加：

```python
@bot.tree.command(name="mycommand", description="描述")
async def my_command(interaction: discord.Interaction, param: str):
    await interaction.response.send_message(f"收到: {param}")
```

### 添加新模型

在 `task_command` 的 `choices` 中添加：

```python
@app_commands.choices(
    model=[
        app_commands.Choice(name="New Model", value="model-id"),
        # ...
    ]
)
```

---

## Windows 适配说明

### 编码处理

Windows 终端常输出 GBK/UTF-8 混杂编码：

```python
buffer += line.decode("utf-8", errors="replace")
```

### Shell 提示注入

自动注入 Windows 环境提示：

```python
windows_prompt = f"""{prompt}
[System Note: You are operating in a Native Windows environment. 
Use PowerShell/CMD syntax instead of Linux commands.]"""
```

### 路径处理

统一使用 `pathlib.Path` 处理跨平台路径：

```python
from pathlib import Path
target_dir = Path(workspaces_dir) / str(thread_id)
```

---

## 故障排查

### Bot 无法读取消息

确保在 Discord Developer Portal 开启了 `Message Content Intent`。

### Droid CLI 未找到

确保 `droid` 命令在系统 PATH 中：

```bash
which droid
# 或
where droid
```

### Git 路径过长错误

Windows 执行（管理员权限）：

```powershell
git config --system core.longpaths true
```

### 任务队列卡住

检查 Worker 是否正常运行：

```python
# 查看队列长度
await status_command(interaction)
```

---

## 许可证

MIT License

---

## 更新日志

### v2.0.0
- 支持多场景任务模式（repo/workspace/temp/qa）
- 新增任务持久化（SQLite）
- 支持任务恢复继续对话
- 智能风险分级审批
- 重构代码结构，模块化设计

---

**Cerebro** — 让 AI 协作更简单 🧠
