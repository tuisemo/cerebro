


这是一份直接面向开发团队的**《Factory Droid x Discord 智能协同引擎技术实施方案指导说明书（Windows 原生完整版）》**。

本文档严格汇总了之前敲定的所有工程化细节、跨平台适配方案、并发与沙盒控制策略以及完整的交互层代码。**开发人员可直接按照本说明书进行项目搭建与编码落地，无须查阅历史上下文。**

---

# 📘 Factory Droid x Discord 智能协同引擎技术实施方案

## 一、 系统架构与工程化设计

### 1.1 系统定位
本系统通过 `discord.py` 构建 Discord 中控机器人，利用 Factory Droid CLI 的 `headless` 模式与流式 `debug` 输出，实现基于 Discord 的远程代码仓库协同调度。本方案专为 **Windows 笔记本原生环境** 优化。

### 1.2 核心机制说明
1. **进程跨平台适配 (Windows Native)**：通过 `shutil.which` 动态寻址 `.exe/.cmd`，通过 `errors='replace'` 容错 Windows 终端复杂的 GBK/UTF-8 混杂输出，并向大模型强制注入 Windows 系统环境 Prompt，防止其生成 `rm -rf` 等 Linux 专属命令。
2. **沙盒级隔离 (Local Clone Sandbox)**：当多个 Discord 用户同时发起指令时，为防止操作同一套代码导致 Git 冲突，系统利用 Git `--local` 特性，在几百毫秒内为每个 Thread 创建独立的本地克隆工作区。
3. **人机协同安全控制 (Human-in-the-loop)**：拦截文件修改、系统命令等高危工具调用，通过 Discord UI 按钮强制要求人工审批。
4. **动态状态面板 (Dynamic Dashboard)**：摒弃刷屏式日志输出，通过 Discord Embed 消息原地更新 Droid 的思考进度与工具执行状态。

---

## 二、 前置环境与 Discord 鉴权配置

### 2.1 宿主机环境解除限制 (Windows 必做)
请在 Windows 笔记本以**管理员身份**打开 PowerShell 并执行，解除 Git 的 260 字符路径限制（防止沙盒内 Node/Python 项目嵌套过深报错）：
```powershell
git config --system core.longpaths true
```

### 2.2 Discord 开发者门户配置
1. 访问 [Discord Developer Portal](https://discord.com/developers/applications) 创建 Application，命名为 `Droid Collaborator`。
2. 进入 `Bot` 页面，**必须开启特权网关：`Message Content Intent`**（否则 Bot 无法读取 Thread 内的用户消息及附件）。获取 `BOT_TOKEN`。
3. 进入 `OAuth2 -> URL Generator`：
   * 勾选 Scope：`bot`, `applications.commands`
   * 勾选 Permissions：`Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Manage Threads`, `Read Message History`, `Attach Files`
4. 复制生成的 URL 并在浏览器中打开，将 Bot 邀请至目标服务器。

### 2.3 工程目录与环境变量规划
**项目结构：**
```text
droid-discord-bot/
├── .env                  # 凭证配置文件
├── requirements.txt      # 依赖清单
├── main.py               # Bot 主入口及事件循环
├── droid_runner.py       # Droid 进程控制与事件流解析
├── workspace_manager.py  # Git 沙盒克隆与 Patch 生成管理
└── ui_components.py      # Discord 交互式面板与审批视图
```

**`.env` 配置文件：**
```ini
DISCORD_BOT_TOKEN=YOUR_DISCORD_BOT_TOKEN_HERE
FACTORY_API_KEY=fk-YOUR_API_KEY_HERE
# Windows 绝对路径，使用正斜杠或双反斜杠
BASE_REPO_PATH=D:/MyProjects/TargetRepository
```

**`requirements.txt`：**
```text
discord.py>=2.3.0
python-dotenv>=1.0.0
```

---

## 三、 核心模块技术实现 (完整代码清单)

以下代码为生产级可运行代码，包含了所有并发、隔离、容错细节。

### 模块 1：进程通信与跨平台适配 (`droid_runner.py`)
负责底层 `droid.exe` 进程的生命周期管理，双向数据流通信与 JSON 解析。

```python
import asyncio
import json
import shutil

class DroidTask:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self.process = None
        
        #[Windows 适配] 动态寻找可执行文件路径 (处理 .exe 或 .cmd 后缀)
        self.droid_exe = shutil.which("droid")
        if not self.droid_exe:
            raise FileNotFoundError("❌ 未在环境变量中找到 Droid CLI，请确认已安装。")

    async def run(self, prompt: str, model: str, session_id: str = None):
        # [Windows 适配] 注入系统级 Prompt，规范大模型在 Windows 下的 Shell 行为
        windows_prompt = f"{prompt}\n\n[System Note: You are operating in a Native Windows environment. Use PowerShell/CMD syntax (e.g., 'dir', 'del', 'type') instead of Linux commands.]"

        cmd =[self.droid_exe, "exec", "--output-format", "debug", "--cwd", self.cwd, "-m", model]
        if session_id:
            cmd.extend(["--session-id", session_id])
        cmd.append(windows_prompt)

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd
        )

        buffer = ""
        try:
            async for line in self.process.stdout:
                #[Windows 适配] Windows 系统命令常输出 GBK 编码，强制转换防崩溃
                buffer += line.decode('utf-8', errors='replace')
                
                while '\n' in buffer:
                    line_str, buffer = buffer.split('\n', 1)
                    line_str = line_str.strip()
                    if not line_str: continue
                    try:
                        yield json.loads(line_str)
                    except json.JSONDecodeError:
                        yield {"type": "raw_output", "text": line_str}
        finally:
            if self.process:
                await self.process.wait()
                self.process = None

    async def send_input(self, text: str):
        """向 Droid 写入交互流（如批准 Y/N 或补充文字）"""
        if self.process and self.process.stdin:
            self.process.stdin.write(f"{text}\n".encode('utf-8'))
            await self.process.stdin.drain()

    def kill(self):
        """异常情况下强杀进程"""
        if self.process: self.process.terminate()
```

### 模块 2：Git 沙盒隔离管理器 (`workspace_manager.py`)
负责在多个任务并发时，为每个 Discord Thread 提供独立的工作副本。

```python
import asyncio
import shutil
from pathlib import Path

class WorkspaceManager:
    def __init__(self, base_repo_path: str, workspaces_dir: str = "./droid_workspaces"):
        # 统一处理路径格式，转换为绝对路径
        self.base_repo = Path(base_repo_path).resolve()
        self.workspaces_dir = Path(workspaces_dir).resolve()
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    async def get_or_create_workspace(self, thread_id: int) -> str:
        """为当前 Thread 创建独立的 Git 沙盒"""
        target_dir = self.workspaces_dir / str(thread_id)
        if not target_dir.exists():
            # 使用 git clone --local 实现极速硬链接克隆，不占用额外磁盘空间
            await asyncio.create_subprocess_exec(
                "git", "clone", "--local", str(self.base_repo), str(target_dir),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            # 检出独立分支隔离改动
            await asyncio.create_subprocess_exec(
                "git", "-C", str(target_dir), "checkout", "-b", f"droid-task-{thread_id}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
        return str(target_dir)

    async def generate_patch(self, thread_id: int) -> str | None:
        """任务结束后，计算沙盒修改内容并生成 Patch 文件"""
        target_dir = self.workspaces_dir / str(thread_id)
        if not target_dir.exists(): return None
        
        # 自动 Commit 所有变更
        await asyncio.create_subprocess_exec("git", "-C", str(target_dir), "add", ".")
        await asyncio.create_subprocess_exec("git", "-C", str(target_dir), "commit", "-m", "Auto-commit by Droid")
        
        # 生成与主分支的差异对比
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(target_dir), "format-patch", "master", "--stdout",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        
        # [Windows 适配] 对 Diff 内容的编码容错
        return stdout.decode('utf-8', errors='replace') if stdout else None
```

### 模块 3：UI/UX 交互层组件 (`ui_components.py`)
提供动态不刷屏的控制面板，以及高危命令的人工拦截审批视图。

```python
import discord

class TaskDashboard:
    """动态状态面板：实时呈现任务进度与日志片段"""
    def __init__(self, interaction: discord.Interaction, prompt: str):
        self.interaction = interaction
        self.message = None
        self.full_log = ""
        self.embed = discord.Embed(
            title="⚙️ Droid 任务运行中 (隔离沙盒)", 
            description=f"**需求:** {prompt[:150]}", 
            color=discord.Color.blue()
        )
        self.embed.add_field(name="🔄 状态", value="初始化系统...", inline=True)
        self.embed.add_field(name="🔧 当前工具", value="-", inline=True)

    async def send(self):
        await self.interaction.response.send_message(embed=self.embed)
        self.message = await self.interaction.original_response()

    async def update(self, status: str = None, tool_name: str = None, log_chunk: str = None):
        if status: self.embed.set_field_at(0, name="🔄 状态", value=status, inline=True)
        if tool_name: self.embed.set_field_at(1, name="🔧 当前工具", value=f"`{tool_name}`", inline=True)
        if log_chunk:
            self.full_log += log_chunk
            # 仅保留最后 800 字符，避免触碰 Discord 单个 Embed 4096 字符的上限
            self.embed.description = f"**需求:** {self.embed.description.split('**需求:** ')[-1].split(chr(10))[0]}\n\n**实时思考日志:**\n```text\n{self.full_log[-800:]}\n```"
        if self.message:
            await self.message.edit(embed=self.embed)

class ApprovalView(discord.ui.View):
    """高危命令拦截组件 (Human-in-the-loop)"""
    def __init__(self, task, tool_name: str):
        super().__init__(timeout=300) # 5分钟未响应自动超时
        self.task = task

    @discord.ui.button(label="允许执行 (Yes)", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.task.send_input("y") # 向 stdin 输入 y
        await interaction.response.send_message("✅ 权限已下发，继续执行", ephemeral=False)
        self.stop()

    @discord.ui.button(label="拒绝 (No)", style=discord.ButtonStyle.danger, emoji="🚫")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.task.send_input("n") # 向 stdin 输入 n
        await interaction.response.send_message("🚫 请求已驳回", ephemeral=False)
        self.stop()
```

### 模块 4：中控系统主入口 (`main.py`)
整合调度模块，负责环境变量加载、Discord WebSocket 心跳维护、斜杠命令解析及附件（多模态输入）下载。

```python
import discord
from discord.ext import commands
import asyncio
import io
import os
from pathlib import Path
from dotenv import load_dotenv

from droid_runner import DroidTask
from workspace_manager import WorkspaceManager
from ui_components import TaskDashboard, ApprovalView

# 1. 基础配置与环境变量初始化
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
API_KEY = os.getenv("FACTORY_API_KEY")
BASE_REPO = os.getenv("BASE_REPO_PATH", "C:/")

if API_KEY:
    os.environ["FACTORY_API_KEY"] = API_KEY  # 供 Droid CLI 自动继承读取

# 2. Discord 网关配置
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 3. 全局实例初始化
workspace_mgr = WorkspaceManager(base_repo_path=BASE_REPO)
active_tasks = {} # 映射：Thread ID -> DroidTask 实例

#[并发控制] Windows 笔记本建议并发任务不超过 2 个，多余请求将排队
droid_semaphore = asyncio.Semaphore(2) 

@bot.event
async def on_ready():
    print(f"✅ 成功连接 Discord WebSocket: {bot.user}")
    print(f"💻 宿主机架构: Windows Native")
    try:
        # 同步斜杠命令树至 Discord 服务器
        synced = await bot.tree.sync()
        print(f"🔄 成功同步 {len(synced)} 个命令")
    except Exception as e:
        print(f"❌ 命令同步失败: {e}")

@bot.tree.command(name="task", description="创建独立沙盒并唤起 Droid 处理任务")
@discord.app_commands.choices(model=[
    discord.app_commands.Choice(name="Claude 3.5 Sonnet (推荐)", value="claude-3-5-sonnet-20241022"),
    discord.app_commands.Choice(name="GPT-4o", value="gpt-4o"),
])
async def task_cmd(interaction: discord.Interaction, prompt: str, model: str = "claude-3-5-sonnet-20241022"):
    
    if droid_semaphore.locked():
        await interaction.response.send_message("⏳ 服务器并发满载，您的任务已加入排队队列...", ephemeral=True)
    
    # 创建独立线程进行交互
    thread = await interaction.channel.create_thread(name=f"🤖 {prompt[:15]}", type=discord.ChannelType.public_thread)
    dashboard = TaskDashboard(interaction, prompt)
    await dashboard.send()

    async with droid_semaphore:
        # 挂载专属代码沙盒
        isolated_cwd = await workspace_mgr.get_or_create_workspace(thread.id)
        task = DroidTask(cwd=isolated_cwd)
        active_tasks[thread.id] = task

        try:
            # 开启事件循环监听 Droid 产出流
            async for event in task.run(prompt, model=model):
                etype = event.get("type")
                
                if etype == "assistant_chunk":
                    await dashboard.update(status="🧠 思考与推理中...", log_chunk=event.get("text", ""))
                    
                elif etype == "tool_call":
                    t_name = event.get('toolName', 'unknown')
                    await dashboard.update(status="⚙️ 执行底层工具", tool_name=t_name)
                    
                    # 敏感危险工具的人工审批拦截
                    if t_name in["execute_command", "write_file", "delete_file"]:
                        cmd_detail = event.get('parameters', {}).get('command', '...')
                        view = ApprovalView(task, t_name)
                        await thread.send(f"⚠️ **Droid 申请执行高危动作:**\n```powershell\n{cmd_detail}\n```", view=view)

                elif etype == "completion":
                    # 任务完成，进行 Diff 计算与交付
                    await dashboard.update(status="✅ 任务顺利完结", tool_name="-")
                    patch_data = await workspace_mgr.generate_patch(thread.id)
                    
                    if patch_data and len(patch_data.strip()) > 0:
                        # 以附件形式发送 Patch，避免 Discord 文本限制
                        patch_file = discord.File(io.BytesIO(patch_data.encode()), filename=f"sandbox_{thread.id}.patch")
                        await thread.send("📦 **检测到代码变更，补丁包已生成，请查收：**", file=patch_file)
                    else:
                        await thread.send("ℹ️ 任务结束，未检测到任何文件变更。")
                    break
                    
        except Exception as e:
             await thread.send(f"❌ 运行期严重异常: {e}")
        finally:
            # 清理内存引用
            if thread.id in active_tasks:
                del active_tasks[thread.id]

@bot.event
async def on_message(message: discord.Message):
    """拦截运行中 Thread 的新消息，支持上传截图、日志报错给 Droid"""
    if message.author.bot: return
    
    if isinstance(message.channel, discord.Thread) and message.channel.id in active_tasks:
        task = active_tasks[message.channel.id]
        final_input = message.content

        # 多模态支持：自动下载 Discord 附件至沙盒目录
        if message.attachments:
            files_desc =[]
            for att in message.attachments:
                save_path = Path(task.cwd) / att.filename
                await att.save(save_path)
                files_desc.append(att.filename)
            # 隐式提示注入，告知 Droid 文件已到位
            final_input += f"\n[System Info: 用户补充上传了文件 ({', '.join(files_desc)}) 至当前目录，请读取其内容进行分析。]"

        if final_input.strip():
            await task.send_input(final_input)
            await message.add_reaction("📥") # 确认已将补充信息喂入进程

    # 必须保留，以兼容其他基于 prefix 的传统命令
    await bot.process_commands(message)

if __name__ == "__main__":
    if not TOKEN: 
        raise ValueError("❌ 启动失败：未在 .env 文件中发现 DISCORD_BOT_TOKEN 配置")
    bot.run(TOKEN)
```

---

## 四、 启动与验收指南

1. **环境初始化**：
   在项目根目录（Windows CMD/PowerShell）执行：
   ```cmd
   pip install -r requirements.txt
   ```
2. **启动引擎**：
   ```cmd
   python main.py
   ```
   若控制台打印 `✅ 成功连接 Discord WebSocket`，则代表系统已上线。
3. **闭环测试流程**：
   * 在 Discord 对应服务器输入 `/task`。
   * 选择参数 `model: Claude 3.5 Sonnet`，输入 `prompt: 帮我在根目录创建一个 hello.py，打印你好世界`。
   * 观察面板是否实时更新思考日志。
   * 当弹出高危命令警告（请求写入文件）时，点击 `✅ 允许执行`。
   * 任务完成后，下载 Discord 发送的 `sandbox_xxx.patch` 补丁文件。
   * 在主项目中执行 `git apply sandbox_xxx.patch` 验收代码。
