"""
Cerebro — 群体智能协同引擎 V2

主入口：Discord Bot 实例、任务队列 Worker、生命周期管理、斜杠命令与消息拦截。
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
from .registry import TaskRegistry


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
BASE_REPO = os.getenv("BASE_REPO_PATH", "./")
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
        thread, prompt, model = await task_queue.get()
        try:
            await _execute(thread, prompt, model)
        except Exception as e:
            logger.error(f"Worker 异常: {e}", exc_info=True)
        finally:
            task_queue.task_done()


async def _execute(thread: discord.Thread, prompt: str, model: str):
    """核心任务执行"""
    dashboard = TaskDashboard(prompt)
    await dashboard.send_to(thread)

    try:
        isolated_cwd = await workspace_mgr.get_or_create_workspace(thread.id)

        task = DroidTask(cwd=isolated_cwd)
        active_tasks[thread.id] = task
        handler = DroidEventHandler(thread, dashboard)

        if task_registry:
            task_registry.register_task(thread.id, isolated_cwd, prompt, model)

        async for event in task.run(prompt, model=model):
            if not await handler.handle(event):
                break

        if task_registry:
            task_registry.update_status(thread.id, "completed")

        patch_data = await workspace_mgr.generate_patch(thread.id)
        if patch_data and patch_data.strip():
            patch_file = discord.File(
                io.BytesIO(patch_data.encode()),
                filename=f"sandbox_{thread.id}.patch",
            )
            await thread.send("📦 **变更已合并至沙盒，补丁包已生成：**", file=patch_file)
        else:
            await thread.send("✨ **任务结束，环境检查完成且无代码变动。**")

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

    # BASE_REPO 校验与 Git 初始化
    if not BASE_REPO:
        logger.error("❌ BASE_REPO_PATH 未配置，请在 .env 中设置 BASE_REPO_PATH")
        await bot.close()
        return

    repo_path = Path(BASE_REPO).resolve()
    if not repo_path.exists():
        repo_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"✅ 已创建目录: {repo_path}")

    # 检查是否为 Git 仓库，不是则自动初始化
    if not (repo_path / ".git").exists():
        logger.info(f"📦 正在为 {repo_path} 初始化 Git 仓库...")
        result = await asyncio.create_subprocess_exec(
            "git", "init",
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await result.communicate()
        if result.returncode == 0:
            logger.info(f"✅ Git 仓库初始化完成: {repo_path}")
        else:
            error_msg = stderr.decode("utf-8", errors="replace") if stderr else "未知错误"
            logger.error(f"❌ Git 初始化失败: {error_msg}")
            await bot.close()
            return
    else:
        logger.info(f"📁 使用已有 Git 仓库: {repo_path}")

    try:
        workspace_mgr = WorkspaceManager(base_repo_path=str(repo_path))
        task_registry = TaskRegistry()

        bot.loop.create_task(workspace_mgr.auto_cleanup_loop(task_registry))
        for _ in range(MAX_CONCURRENT):
            bot.loop.create_task(task_worker())

        logger.info(f"📁 系统就绪 (Workers: {MAX_CONCURRENT}, 仓库: {repo_path})")
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

@bot.tree.command(name="task", description="提交任务至 Cerebro 队列")
@app_commands.choices(
    model=[
        app_commands.Choice(name="Claude 4.6 (Default)", value="claude-opus-4-6"),
        app_commands.Choice(name="Claude Sonnet 4.6", value="claude-sonnet-4-6"),
        app_commands.Choice(name="GPT-5.4 Mini", value="gpt-5.4-mini"),
        app_commands.Choice(name="Gemini 3 Flash", value="gemini-3-flash-preview"),
        app_commands.Choice(name="MiniMax-M2.7", value="custom:MiniMax-M2.7"),
        app_commands.Choice(name="Qwen 3.5 Plus", value="custom:qwen3.5-plus"),
    ]
)
async def task_command(interaction: discord.Interaction, prompt: str, model: str = None):
    if model is None:
        model = DEFAULT_MODEL

    if not workspace_mgr:
        return await interaction.response.send_message("⚠️ 系统未就绪。", ephemeral=True)

    # 确定线程
    if isinstance(interaction.channel, discord.Thread):
        thread = interaction.channel
    else:
        name = f"🤖 {prompt[:15]}..." if len(prompt) > 15 else f"🤖 {prompt}"
        thread = await interaction.channel.create_thread(
            name=name, type=discord.ChannelType.public_thread
        )

    await interaction.response.defer(ephemeral=True)

    queue_pos = task_queue.qsize() + 1
    msg = "✅ 任务已提交。"
    if queue_pos > 1:
        msg = f"⏳ 已入队，当前排第 {queue_pos} 位…"

    await interaction.followup.send(msg, ephemeral=True)
    await task_queue.put((thread, prompt, model))


@bot.tree.command(name="status", description="查看 Cerebro 系统状态")
async def status_command(interaction: discord.Interaction):
    status_msg = (
        f"**Cerebro 实时状态**\n"
        f"活跃: {len(active_tasks)} | 队列: {task_queue.qsize()} | 并发上限: {MAX_CONCURRENT}\n"
        f"仓库: `{BASE_REPO}`"
    )
    await interaction.response.send_message(status_msg, ephemeral=True)


@bot.tree.command(name="cleanup", description="手动清理任务资源")
async def cleanup_command(interaction: discord.Interaction, thread_id: str):
    if not workspace_mgr:
        return await interaction.response.send_message("⚠️ 系统未就绪。", ephemeral=True)
    await workspace_mgr.cleanup_workspace(int(thread_id), registry=task_registry)
    await interaction.response.send_message(f"✅ 已清理: {thread_id}", ephemeral=True)


# ============================================================================
# 消息拦截
# ============================================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if isinstance(message.channel, discord.Thread) and message.channel.id in active_tasks:
        task = active_tasks[message.channel.id]
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
