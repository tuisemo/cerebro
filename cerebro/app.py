"""
Cerebro — 群体智能协同引擎 V2

主入口：Discord Bot 实例、任务队列 Worker、生命周期管理、斜杠命令与消息拦截。
支持多场景任务：无文件系统、临时工作区、指定仓库、指定目录。
"""

import asyncio
import os
import io
import logging
from typing import Optional
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from .runner import DroidTask
from .workspace import WorkspaceManager, WorkspaceError
from .ui import TaskDashboard
from .handler import DroidEventHandler
from .registry import TaskRegistry, STATUS_COMPLETED
from .parser import parse_task_command, format_task_preview


# ============================================================================
# 日志
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("Cerebro")


# ============================================================================
# 配置
# ============================================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
API_KEY = os.getenv("FACTORY_API_KEY", "")
WORKSPACES_DIR = os.getenv("WORKSPACES_DIR", "./droid_workspaces")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "custom:MiniMax-M2.7")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))

if API_KEY:
    os.environ["FACTORY_API_KEY"] = API_KEY


# ============================================================================
# Bot 实例 & 全局状态
# ============================================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

workspace_mgr: Optional[WorkspaceManager] = None
task_registry: Optional[TaskRegistry] = None
active_tasks: dict[int, DroidTask] = {}
task_queue: asyncio.Queue = asyncio.Queue()


# ============================================================================
# Worker
# ============================================================================

async def task_worker():
    """后台任务消费者"""
    logger.info("👷 Worker 已就绪")
    while True:
        task_data = await task_queue.get()
        
        # 支持两种格式：
        # - 新格式: (thread, ParsedTask, model)
        # - 旧格式: (thread, prompt_str, model)
        thread = task_data[0]
        model = task_data[2]
        
        if isinstance(task_data[1], str):
            # 旧格式：第二个元素是字符串 prompt
            prompt = task_data[1]
            parsed = parse_task_command(prompt)
        else:
            # 新格式：第二个元素是 ParsedTask 对象
            parsed = task_data[1]
            prompt = parsed.task
        
        try:
            await _execute(thread, prompt, model, parsed)
        except Exception as e:
            logger.error(f"Worker 异常: {e}", exc_info=True)
        finally:
            task_queue.task_done()


async def _execute(thread: discord.Thread, prompt: str, model: str, parsed=None):
    """核心任务执行"""
    # 解析任务参数
    if parsed is None:
        parsed = parse_task_command(prompt)
    
    dashboard = TaskDashboard(parsed.task)
    await dashboard.send_to(thread)

    try:
        # 获取工作区
        if parsed.repo or parsed.workspace:
            # 模式1/2: 指定仓库或目录
            isolated_cwd = await workspace_mgr.get_workspace(
                thread.id,
                repo_path=parsed.repo,
                workspace_path=parsed.workspace,
                is_file_operation=parsed.is_file_operation,
            )
        else:
            # 模式3/4: 临时工作区或无文件系统
            isolated_cwd = await workspace_mgr.get_workspace(
                thread.id,
                is_file_operation=parsed.is_file_operation,
            )

        # 记录任务信息
        workspace_info = format_task_preview(parsed)
        await thread.send(f"📋 **任务模式:** {workspace_info}")

        task = DroidTask(cwd=isolated_cwd)
        active_tasks[thread.id] = task
        handler = DroidEventHandler(thread, dashboard)

        if task_registry:
            task_registry.register_task(
                thread.id, 
                isolated_cwd, 
                parsed.task, 
                model,
                task_type="git_clone" if parsed.repo else ("workspace" if parsed.workspace else "temp"),
                parsed_data={
                    "repo": parsed.repo,
                    "workspace": parsed.workspace,
                    "is_file_operation": parsed.is_file_operation,
                }
            )

        async for event in task.run(parsed.task, model=model):
            if not await handler.handle(event):
                break

        if task_registry:
            task_registry.update_status(thread.id, STATUS_COMPLETED)

        # 生成 Patch（如有变更）
        if parsed.repo or parsed.is_file_operation:
            patch_data = await workspace_mgr.generate_patch(thread.id)
            if patch_data and patch_data.strip():
                patch_file = discord.File(
                    io.BytesIO(patch_data.encode()),
                    filename=f"sandbox_{thread.id}.patch",
                )
                await thread.send("📦 **变更已合并至沙盒，补丁包已生成：**", file=patch_file)
            else:
                await thread.send("✨ **任务结束，环境检查完成且无代码变动。**")
        else:
            await thread.send("✨ **任务结束。**")

    except Exception as e:
        logger.error(f"任务 {thread.id} 失败: {e}")
        if task_registry:
            task_registry.update_status(thread.id, "error")
        await dashboard.error(str(e))
        await thread.send(f"❌ **任务执行失败:** {e}")

    finally:
        active_tasks.pop(thread.id, None)


# ============================================================================
# 生命周期
# ============================================================================

@bot.event
async def on_ready():
    global workspace_mgr, task_registry

    logger.info(f"✅ 已连接: {bot.user}")

    try:
        workspace_mgr = WorkspaceManager(workspaces_dir=WORKSPACES_DIR)
        task_registry = TaskRegistry()

        bot.loop.create_task(workspace_mgr.auto_cleanup_loop(task_registry))
        for _ in range(MAX_CONCURRENT):
            bot.loop.create_task(task_worker())

        logger.info(f"📁 系统就绪 (Workers: {MAX_CONCURRENT}, 工作区: {WORKSPACES_DIR})")
    except Exception as e:
        logger.error(f"初始化失败: {e}")
        await bot.close()
        return

    try:
        synced = await bot.tree.sync()
        logger.info(f"🔄 同步 {len(synced)} 命令")
    except Exception as e:
        logger.error(f"命令同步失败: {e}")


# ============================================================================
# 斜杠命令
# ============================================================================

@bot.tree.command(
    name="task", 
    description="提交任务至 Cerebro 队列。支持: /task 描述 repo:仓库路径 workspace:工作目录"
)
@app_commands.choices(
    model=[
        app_commands.Choice(name="MiniMax-M2.7 (默认)", value="custom:MiniMax-M2.7"),
        app_commands.Choice(name="Claude 4.6", value="claude-opus-4-6"),
        app_commands.Choice(name="Claude Sonnet 4.6", value="claude-sonnet-4-6"),
        app_commands.Choice(name="GPT-5.4 Mini", value="gpt-5.4-mini"),
        app_commands.Choice(name="Gemini 3 Flash", value="gemini-3-flash-preview"),
        app_commands.Choice(name="Qwen 3.5 Plus", value="custom:qwen3.5-plus"),
    ]
)
async def task_command(interaction: discord.Interaction, prompt: str, model: str = None):
    """任务命令，支持 repo: 和 workspace: 参数"""
    if model is None:
        model = DEFAULT_MODEL

    if not workspace_mgr:
        return await interaction.response.send_message("⚠️ 系统未就绪。", ephemeral=True)

    # 解析指令
    parsed = parse_task_command(prompt)

    # 确定线程
    if isinstance(interaction.channel, discord.Thread):
        thread = interaction.channel
    else:
        name = f"🤖 {parsed.task[:15]}..." if len(parsed.task) > 15 else f"🤖 {parsed.task}"
        thread = await interaction.channel.create_thread(
            name=name, type=discord.ChannelType.public_thread
        )

    await interaction.response.defer(ephemeral=True)

    # 预览任务配置
    preview = format_task_preview(parsed)
    queue_pos = task_queue.qsize() + 1
    msg = f"✅ 任务已提交。\n{preview}"
    if queue_pos > 1:
        msg = f"⏳ 已入队，当前排第 {queue_pos} 位。\n{preview}"

    await interaction.followup.send(msg, ephemeral=True)
    await task_queue.put((thread, parsed, model))


@bot.tree.command(name="status", description="查看 Cerebro 系统状态")
async def status_command(interaction: discord.Interaction):
    status_msg = (
        f"**Cerebro 实时状态**\n"
        f"活跃: {len(active_tasks)} | 队列: {task_queue.qsize()} | 并发上限: {MAX_CONCURRENT}\n"
        f"工作区: `{WORKSPACES_DIR}`"
    )
    await interaction.response.send_message(status_msg, ephemeral=True)


@bot.tree.command(name="cleanup", description="手动清理任务资源")
async def cleanup_command(interaction: discord.Interaction, thread_id: str):
    if not workspace_mgr:
        return await interaction.response.send_message("⚠️ 系统未就绪。", ephemeral=True)
    await workspace_mgr.cleanup_workspace(int(thread_id), registry=task_registry)
    await interaction.response.send_message(f"✅ 已清理: {thread_id}", ephemeral=True)


@bot.tree.command(name="new", description="开启全新会话，清理上一个任务的上下文")
async def new_command(interaction: discord.Interaction):
    """清理当前 Thread 的工作区，开启新会话"""
    if not isinstance(interaction.channel, discord.Thread):
        return await interaction.response.send_message("⚠️ /new 命令需要在 Thread 中使用。", ephemeral=True)

    if not workspace_mgr or not task_registry:
        return await interaction.response.send_message("⚠️ 系统未就绪。", ephemeral=True)

    thread_id = interaction.channel.id

    # 清理旧工作区
    await workspace_mgr.cleanup_workspace(thread_id, registry=task_registry)

    await interaction.response.send_message("🆕 全新会话已开启，工作区已清理。", ephemeral=True)


# ============================================================================
# 消息拦截
# ============================================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not isinstance(message.channel, discord.Thread):
        return

    thread_id = message.channel.id

    # 活跃任务：直接转发消息
    if thread_id in active_tasks:
        task = active_tasks[thread_id]
        user_input = message.content

        if message.attachments:
            f_list = []
            for att in message.attachments:
                save_path = Path(task.cwd) / att.filename
                await att.save(save_path)
                f_list.append(att.filename)
            user_input += f"\n[System Info: 已收到上传文件: {', '.join(f_list)}]"

        if user_input.strip():
            await task.send_input(user_input)
            await message.add_reaction("📥")
        return

    # 已完成任务：检查是否可继续
    if task_registry and task_registry.is_resumable(thread_id):
        # 自动继续任务
        task_info = task_registry.get_task_by_thread(thread_id)
        parsed_data = task_info.get("parsed_data", {})
        
        # 构建继续的 prompt
        continue_prompt = message.content
        if continue_prompt.strip():
            await message.channel.send("🔄 继续上一个任务...")
            
            # 使用保存的 parsed_data 重建 ParsedTask
            from .parser import ParsedTask
            parsed = ParsedTask(
                task=continue_prompt,
                repo=parsed_data.get("repo"),
                workspace=parsed_data.get("workspace"),
                is_file_operation=parsed_data.get("is_file_operation", False),
            )
            
            await task_queue.put((message.channel, parsed, task_info["model"]))
            await message.add_reaction("📥")

    await bot.process_commands(message)


# ============================================================================
# 入口
# ============================================================================

def main():
    if not TOKEN:
        logger.error("❌ 未发现 DISCORD_BOT_TOKEN")
        return
    logger.info("🚀 启动 Cerebro 引擎…")
    bot.run(TOKEN, log_level=logging.WARNING)


if __name__ == "__main__":
    main()
