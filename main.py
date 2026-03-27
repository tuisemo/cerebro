"""
Factory Droid x Discord 智能协同引擎

主入口：整合调度模块，负责环境变量加载、Discord WebSocket 心跳维护、
斜杠命令解析及附件（多模态输入）下载。
"""

import asyncio
import io
import os
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from droid_runner import DroidTask, DroidProcessError
from workspace_manager import WorkspaceManager, WorkspaceError
from ui_components import TaskDashboard, ApprovalView


# ============================================================================
# 配置与环境变量
# ============================================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
API_KEY = os.getenv("FACTORY_API_KEY", "")
BASE_REPO = os.getenv("BASE_REPO_PATH", "./")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-3-5-sonnet-20241022")

# 设置 API Key 环境变量供 Droid CLI 自动继承读取
if API_KEY:
    os.environ["FACTORY_API_KEY"] = API_KEY


# ============================================================================
# Discord 网关配置
# ============================================================================

intents = discord.Intents.default()
intents.message_content = True  # 必须开启才能读取消息内容

bot = commands.Bot(command_prefix="!", intents=intents)


# ============================================================================
# 全局实例初始化
# ============================================================================

# 工作区管理器（延迟初始化，等 BASE_REPO 配置好）
workspace_mgr: Optional[WorkspaceManager] = None

# 活跃任务映射：Thread ID -> DroidTask 实例
active_tasks: dict[int, DroidTask] = {}

# [并发控制] Windows 笔记本建议并发任务不超过 2 个
droid_semaphore = asyncio.Semaphore(2)


# ============================================================================
# 生命周期事件
# ============================================================================

@bot.event
async def on_ready():
    """Bot 启动成功后的回调"""
    global workspace_mgr

    print(f"✅ 成功连接 Discord WebSocket: {bot.user}")
    print(f"💻 宿主机架构: Windows Native")

    # 初始化工作区管理器
    if BASE_REPO:
        try:
            workspace_mgr = WorkspaceManager(base_repo_path=BASE_REPO)
            print(f"📁 工作区管理器已就绪: {BASE_REPO}")
        except WorkspaceError as e:
            print(f"⚠️ 工作区管理器初始化失败: {e}")
            workspace_mgr = None
    else:
        print("⚠️ 未配置 BASE_REPO_PATH，部分功能可能受限")

    # 同步斜杠命令树至 Discord 服务器
    try:
        synced = await bot.tree.sync()
        print(f"🔄 成功同步 {len(synced)} 个命令")
    except Exception as e:
        print(f"❌ 命令同步失败: {e}")


# ============================================================================
# 斜杠命令
# ============================================================================

@bot.tree.command(
    name="task",
    description="创建独立沙盒并唤起 Droid 处理任务",
)
@app_commands.choices(
    model=[
        app_commands.Choice(
            name="Claude 3.5 Sonnet (推荐)",
            value="claude-3-5-sonnet-20241022",
        ),
        app_commands.Choice(
            name="GPT-4o",
            value="gpt-4o",
        ),
    ]
)
async def task_command(
    interaction: discord.Interaction,
    prompt: str,
    model: str = None,
):
    """
    主任务命令：创建独立线程并启动 Droid 处理

    Args:
        interaction: Discord 交互对象
        prompt: 用户输入的指令
        model: 使用的 AI 模型
    """
    # 使用默认模型（如果用户未指定）
    if model is None:
        model = DEFAULT_MODEL

    # 检查工作区管理器是否就绪
    if not workspace_mgr:
        await interaction.response.send_message(
            "⚠️ 工作区管理器未初始化，请检查 BASE_REPO_PATH 配置。",
            ephemeral=True,
        )
        return

    # 并发控制
    if droid_semaphore.locked():
        await interaction.response.send_message(
            "⏳ 服务器并发满载，您的任务已加入排队队列...",
            ephemeral=True,
        )

    # 如果当前 channel 已经是 Thread，直接使用；否则创建新线程
    if isinstance(interaction.channel, discord.Thread):
        thread = interaction.channel
    else:
        thread_name = f"🤖 {prompt[:15]}..." if len(prompt) > 15 else f"🤖 {prompt}"
        thread = await interaction.channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
        )

    # 创建并发送初始状态面板
    dashboard = TaskDashboard(interaction, prompt)
    await dashboard.send()

    async with droid_semaphore:
        # 为当前 Thread 挂载专属代码沙盒
        try:
            isolated_cwd = await workspace_mgr.get_or_create_workspace(thread.id)
        except WorkspaceError as e:
            await dashboard.error(f"工作区创建失败: {e}")
            await thread.send(f"❌ 工作区创建失败: {e}")
            return

        task = DroidTask(cwd=isolated_cwd)
        active_tasks[thread.id] = task

        try:
            # 开启事件循环监听 Droid 产出流
            async for event in task.run(prompt, model=model):
                etype = event.get("type", "")

                if etype in ("assistant_chunk", "thinking"):
                    await dashboard.update(
                        status="🧠 思考与推理中...",
                        log_chunk=event.get("text", ""),
                    )

                elif etype == "tool_call":
                    tool_name = event.get("toolName", "unknown")
                    await dashboard.update(
                        status="⚙️ 执行底层工具",
                        tool_name=tool_name,
                    )

                    # 敏感危险工具的人工审批拦截
                    dangerous_tools = ["execute_command", "write_file", "delete_file", "edit_file"]
                    if tool_name in dangerous_tools:
                        cmd_detail = event.get("parameters", {}).get("command", "...")
                        view = ApprovalView(task, tool_name, cmd_detail)
                        await thread.send(
                            f"⚠️ **Droid 申请执行高危动作:**\n```\n{cmd_detail}\n```",
                            view=view,
                        )

                elif etype == "tool_result":
                    tool_name = event.get("toolName", "unknown")
                    result_preview = str(event.get("result", ""))[:200]
                    await dashboard.update(
                        status=f"✅ {tool_name} 执行完成",
                        log_chunk=f"[{tool_name}] {result_preview}\n",
                    )

                elif etype == "completion":
                    # 任务完成，进行 Diff 计算与交付
                    await dashboard.complete()
                    patch_data = await workspace_mgr.generate_patch(thread.id)

                    if patch_data and len(patch_data.strip()) > 0:
                        # 以附件形式发送 Patch，避免 Discord 文本限制
                        patch_file = discord.File(
                            io.BytesIO(patch_data.encode()),
                            filename=f"sandbox_{thread.id}.patch",
                        )
                        await thread.send(
                            "📦 **检测到代码变更，补丁包已生成，请查收：**",
                            file=patch_file,
                        )
                    else:
                        await thread.send("ℹ️ 任务结束，未检测到任何文件变更。")
                    break

                elif etype == "error":
                    error_msg = event.get("text", "未知错误")
                    await dashboard.error(error_msg)
                    await thread.send(f"❌ Droid 执行出错: {error_msg}")
                    break

                elif etype == "raw_output":
                    # 处理非 JSON 格式的原始输出
                    await dashboard.update(
                        status="📤 正在输出...",
                        log_chunk=f"{event.get('text', '')}\n",
                    )

        except Exception as e:
            await dashboard.error(str(e))
            await thread.send(f"❌ 运行期严重异常: {e}")

        finally:
            # 清理内存引用
            if thread.id in active_tasks:
                del active_tasks[thread.id]


@bot.tree.command(
    name="status",
    description="查看当前工作区和任务状态",
)
async def status_command(interaction: discord.Interaction):
    """查看当前机器人的工作状态"""
    global workspace_mgr

    status_lines = [
        f"**Droid Collaborator 状态面板**",
        f"",
        f"🤖 机器人: {bot.user}",
        f"📁 工作区: {BASE_REPO or '未配置'}",
        f"🔧 活跃任务: {len(active_tasks)}",
    ]

    if workspace_mgr:
        workspaces = list(workspace_mgr.workspaces_dir.glob("*"))
        status_lines.append(f"📂 已创建工作区: {len(workspaces)}")

    await interaction.response.send_message("\n".join(status_lines), ephemeral=True)


@bot.tree.command(
    name="cleanup",
    description="清理指定 Thread 的工作区",
)
async def cleanup_command(
    interaction: discord.Interaction,
    thread_id: int,
):
    """手动清理指定 Thread 的工作区"""
    global workspace_mgr

    if not workspace_mgr:
        await interaction.response.send_message(
            "⚠️ 工作区管理器未初始化。",
            ephemeral=True,
        )
        return

    await workspace_mgr.cleanup_workspace(thread_id)
    await interaction.response.send_message(
        f"✅ 已清理工作区: {thread_id}",
        ephemeral=True,
    )


# ============================================================================
# 消息事件处理
# ============================================================================

@bot.event
async def on_message(message: discord.Message):
    """
    拦截运行中 Thread 的新消息，支持上传截图、日志报错给 Droid
    """
    # 忽略机器人消息
    if message.author.bot:
        return

    # 检查是否是活跃任务所在的 Thread
    if isinstance(message.channel, discord.Thread) and message.channel.id in active_tasks:
        task = active_tasks[message.channel.id]
        final_input = message.content

        # 多模态支持：自动下载 Discord 附件至沙盒目录
        if message.attachments:
            files_desc = []
            for att in message.attachments:
                save_path = Path(task.cwd) / att.filename
                await att.save(save_path)
                files_desc.append(att.filename)

            # 隐式提示注入，告知 Droid 文件已到位
            final_input += (
                f"\n[System Info: 用户补充上传了文件 ({', '.join(files_desc)}) "
                f"至当前目录，请读取其内容进行分析。]"
            )

        if final_input.strip():
            await task.send_input(final_input)
            await message.add_reaction("📥")  # 确认已将补充信息喂入进程

    # 必须保留，以兼容其他基于 prefix 的传统命令
    await bot.process_commands(message)


# ============================================================================
# 启动入口
# ============================================================================

def main():
    """主入口函数"""
    if not TOKEN:
        raise ValueError(
            "❌ 启动失败：未在 .env 文件中发现 DISCORD_BOT_TOKEN 配置\n"
            "请参考 .env.example 创建配置文件。"
        )

    print("🚀 启动 Droid Discord 引擎...")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
