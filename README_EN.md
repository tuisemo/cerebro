# Cerebro — Swarm Intelligence Collaboration Engine

> A Discord-based Factory Droid CLI orchestration bot supporting multi-scenario task modes, intelligent workspace isolation, and streaming interactions.

## Project Overview

Cerebro is a Discord Bot that enables remote code repository collaboration through Factory Droid CLI's headless mode. It supports multi-user concurrency, workspace isolation, intelligent task parsing, and real-time status feedback.

### Core Features

- **Multi-Scenario Task Modes**: Supports repo cloning, specified workspace, temp directory, and Q&A modes
- **Intelligent Workspace Isolation**: Each Discord Thread has an independent workspace to prevent Git conflicts
- **Concurrent Task Queue**: Configurable concurrency limit with automatic queuing for excess tasks
- **Streaming Status Panel**: Real-time task status updates without spamming the channel
- **Human-in-the-Loop Approval**: High-risk commands are intercepted and require user confirmation
- **Task Persistence**: SQLite storage for task states, supporting crash recovery and conversation continuation

---

## Quick Start

### Requirements

- Python >= 3.12
- Windows (native environment adapted)
- Git (requires `core.longpaths true` configuration)

### Install Dependencies

```bash
# Using uv (recommended)
uv sync

# Or using pip
pip install -e .
```

### Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` file:

```ini
# Discord Bot Token (required)
DISCORD_BOT_TOKEN=your_bot_token_here

# Factory API Key (optional, if already in system environment)
FACTORY_API_KEY=fk-your-api-key

# Default Model (optional)
DEFAULT_MODEL=custom:MiniMax-M2.7

# Workspaces Directory (optional, default ./droid_workspaces)
WORKSPACES_DIR=./droid_workspaces

# Concurrent Tasks Limit (optional, default 2)
MAX_CONCURRENT_TASKS=2
```

### Discord Bot Configuration

1. Visit [Discord Developer Portal](https://discord.com/developers/applications) and create an Application
2. Go to **Bot** page, enable `Message Content Intent`
3. Copy **BOT_TOKEN** to `.env`
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Manage Threads`, `Read Message History`, `Attach Files`
5. Copy the generated URL and invite the Bot to your server

### Launch the Bot

```bash
# Method 1: Using uv
uv run cerebro

# Method 2: Using Python module
python -m cerebro

# Method 3: Direct execution
python cerebro/app.py
```

---

## Usage Guide

### Basic Commands

```
/task <description> [repo:<path>] [workspace:<path>]
```

#### Scenario Examples

| Scenario | Command |
|----------|---------|
| Q&A Only | `/task explain what decorators are` |
| Repo Mode | `/task refactor login module repo:D:/Projects/MyApp` |
| Specified Workspace | `/task write a backup script workspace:D:/scripts` |
| Temp Directory | `/task create a Flask application` |

### Available Slash Commands

| Command | Description |
|---------|-------------|
| `/task <description> [repo/workspace]` | Submit a task to the queue |
| `/status` | View system status (active tasks, queue length) |
| `/new` | Start a fresh session in current Thread, clean up workspace |
| `/cleanup <thread_id>` | Manually clean up resources for a specific task |

### Supported Models

- MiniMax-M2.7 (default)
- Claude 4.6 / Sonnet 4.6
- GPT-5.4 Mini
- Gemini 3 Flash
- Qwen 3.5 Plus

---

## Project Structure

```
cerebro/
├── __init__.py      # Version info
├── __main__.py      # Module entry (python -m cerebro)
├── app.py           # Bot main entry, command definitions, message interception
├── runner.py        # Droid CLI process control and event stream parsing
├── handler.py       # Droid event handler, mapping to Discord interactions
├── workspace.py     # Workspace manager (Git cloning, sandbox isolation)
├── registry.py      # SQLite task persistence and state management
├── parser.py        # Task command parser (repo/workspace parameter extraction)
├── ui.py            # Discord status panel (TaskDashboard)
└── throttle.py      # Message rate limiter
```

### Core Module Descriptions

#### `app.py` — Bot Main Entry

- Discord Bot instance initialization
- Slash command definitions (`/task`, `/status`, `/new`, `/cleanup`)
- Background Worker task queue consumer
- Message interception and multimodal attachment handling
- Task lifecycle management

#### `runner.py` — Droid Process Control

- Dynamically locate `droid` executable
- **Dual-transport architecture** (Phase 3+):
  - `CliDroidTransport` — spawns `droid exec` subprocess, parses JSON event stream
  - `SdkDroidTransport` — uses `droid-sdk` Python client, supports permission and ask-user callbacks
  - `BaseDroidTransport` — shared abstract base for both transports
- `InteractionBridge` decouples permission/ask-user callbacks, allowing any interaction layer to be plugged in
- Windows environment adaptation (encoding tolerance, shell prompt injection)

#### `workspace.py` — Workspace Management

Supports three workspace modes:
- **Repo Mode**: `git clone --local` specified repository
- **Workspace Mode**: Use specified directory directly
- **Temp Mode**: Create temporary Git repository

Auto-generates code change patches, supports automatic cleanup of expired workspaces.

#### `handler.py` — Event Handler

Maps Droid CLI's JSON event stream to Discord interactions:
- `assistant_chunk` → Message buffering and segmented sending
- `tool_call` → Tool execution status notification
- Smart risk classification: High-risk commands block for confirmation, moderate-risk auto-notify

#### `parser.py` — Command Parser

Parses `/task` command parameters:
```python
parse_task_command("refactor login repo:D:/MyApp")
# → ParsedTask(task="refactor login", repo="D:/MyApp", is_file_operation=True)
```

Auto-detects file operation keywords to determine if workspace creation is needed.

#### `registry.py` — Task Persistence

SQLite storage:
- Task metadata (Thread ID, workspace path, model, type)
- Status tracking (active → completed → closed)
- Supports task resumption (completed tasks can continue conversation)
- Automatic cleanup of expired workspaces

---

## Technical Architecture

### Data Flow

```
Discord User
     ↓
[ /task command ]
     ↓
parser.py (parameter parsing)
     ↓
asyncio.Queue (task queue)
     ↓
task_worker (concurrent worker)
     ↓
workspace.py (get/create workspace)
     ↓
runner.py
 ├─ CliDroidTransport   (DROID_TRANSPORT=cli, default)
 │   └─ subprocess "droid exec" + JSON event parsing
 └─ SdkDroidTransport   (DROID_TRANSPORT=sdk, Phase 3+)
     └─ droid-sdk Python client + Discord permission bridging
     ↓
handler.py (event stream processing)
     ↓
Discord Thread (message/Embed updates)
```

### Concurrency Control

```python
MAX_CONCURRENT_TASKS = 2  # Configurable
droid_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
```

Excess tasks automatically queue, Worker loop consumes from queue.

### Workspace Isolation

Each Thread ID corresponds to an independent workspace:
```
./droid_workspaces/
├── 123456789/          # Thread ID
│   ├── .git/
│   └── ...
└── 987654321/
    └── ...
```

### Risk Classification

| Level | Command Patterns | Handling |
|-------|------------------|----------|
| High | `rm -rf`, `del /`, `format`, etc. | Discord button block confirmation |
| Moderate | `Execute`, `execute_command` | Send notification, auto-continue after 3s |
| Normal | Other tools | Direct execution |

---

## Configuration Options

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | ✅ | - | Discord Bot Token |
| `FACTORY_API_KEY` | ❌ | - | Factory API Key |
| `DEFAULT_MODEL` | ❌ | `custom:MiniMax-M2.7` | Default AI Model |
| `DROID_TRANSPORT` | ❌ | `cli` | Droid transport layer: `cli` (subprocess, default) or `sdk` (Python SDK client, Phase 3+) |
| `WORKSPACES_DIR` | ❌ | `./droid_workspaces` | Workspace root directory |
| `MAX_CONCURRENT_TASKS` | ❌ | `2` | Concurrent task limit |

### pyproject.toml

```toml
[project]
name = "cerebro"
version = "2.1.0"
description = "Cerebro — Swarm Intelligence Collaboration Engine"
requires-python = ">=3.12"
dependencies = [
    "discord.py>=2.3.0",
    "droid-sdk>=0.1.2",        # Phase 3 SDK transport (optional, required when DROID_TRANSPORT=sdk)
    "python-dotenv>=1.0.0",
]

[project.scripts]
cerebro = "cerebro.app:main"
```

---

## Development Guide

### Local Development

```bash
# Install development dependencies
uv pip install -e "."

# Run tests (if any)
pytest

# Code formatting
ruff format cerebro/

# Type checking
mypy cerebro/
```

### Adding New Commands

Add to `app.py`:

```python
@bot.tree.command(name="mycommand", description="Description")
async def my_command(interaction: discord.Interaction, param: str):
    await interaction.response.send_message(f"Received: {param}")
```

### Adding New Models

Add to `task_command`'s `choices`:

```python
@app_commands.choices(
    model=[
        app_commands.Choice(name="New Model", value="model-id"),
        # ...
    ]
)
```

---

## Windows Adaptation Notes

### Encoding Handling

Windows terminals often output mixed GBK/UTF-8 encoding:

```python
buffer += line.decode("utf-8", errors="replace")
```

### Shell Prompt Injection

Auto-inject Windows environment prompt:

```python
windows_prompt = f"""{prompt}
[System Note: You are operating in a Native Windows environment. 
Use PowerShell/CMD syntax instead of Linux commands.]"""
```

### Path Handling

Use `pathlib.Path` for cross-platform path handling:

```python
from pathlib import Path
target_dir = Path(workspaces_dir) / str(thread_id)
```

---

## Troubleshooting

### Bot Cannot Read Messages

Ensure `Message Content Intent` is enabled in Discord Developer Portal.

### Droid CLI Not Found

Ensure `droid` command is in system PATH:

```bash
which droid
# or
where droid
```

### Git Path Too Long Error

Execute in Windows (administrator privileges):

```powershell
git config --system core.longpaths true
```

### Task Queue Stuck

Check if Worker is running properly:

```python
# View queue length
await status_command(interaction)
```

---

## License

MIT License

---

## Changelog

> Full changelog available at [CHANGELOG.md](./CHANGELOG.md).

### v2.1.0 — SDK Transport Pilot (Phase 3–4)
- New `DROID_TRANSPORT` environment variable, supporting `sdk` transport layer (Python SDK client)
- SDK Permission Bridging: tool permission requests approved via Discord button/notification interactions
- SDK ask-user Bridging: supplementary information requested directly in Discord Thread replies
- Dual-transport architecture: `CliDroidTransport` (subprocess) + `SdkDroidTransport` (Python SDK)
- `InteractionBridge` decouples transport from Discord interaction logic
- `flush_output()` public method fixes buffer flushing on completion events

### v2.0.0
- Added multi-scenario task modes (repo/workspace/temp/qa)
- Added task persistence (SQLite)
- Support conversation continuation for completed tasks
- Smart risk classification approval
- Refactored code structure, modular design

---

**Cerebro** — Making AI Collaboration Easier 🧠
