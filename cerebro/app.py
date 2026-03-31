"""
Cerebro — 群体智能协同引擎 V2

主入口：Discord Bot 实例、任务队列 Worker、生命周期管理、斜杠命令与消息拦截。
支持多场景任务：无文件系统、临时工作区、指定仓库、指定目录。
"""

import asyncio
import os
import io
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from .runner import DroidTask, InteractionBridge, get_droid_transport_name
from .workspace import WorkspaceManager, WorkspaceError
from .ui import TaskDashboard
from .handler import DroidEventHandler
from .registry import TaskRegistry, STATUS_COMPLETED, STATUS_WAITING
from .parser import parse_task_command, format_task_preview


# ============================================================================
# 日志
# ============================================================================

LOG_DIR = os.getenv("LOG_DIR", "./logs")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 默认 10MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "7"))  # 默认保留 7 天

import pathlib
log_path = pathlib.Path(LOG_DIR)
log_path.mkdir(parents=True, exist_ok=True)

root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# 文件输出：按日期归档，每日一个文件
file_handler = logging.FileHandler(
    log_path / f"cerebro_{datetime.now().strftime('%Y%m%d')}.log",
    encoding="utf-8",
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
))

# 控制台输出
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)
console_handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
))

root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

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
TASK_TIMEOUT_MINUTES = int(os.getenv("TASK_TIMEOUT_MINUTES", "10"))  # 任务超时时间（分钟）
DROID_TRANSPORT = get_droid_transport_name()

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
task_queue: asyncio.Queue = asyncio.Queue(maxsize=100)


@dataclass
class PendingAskUserRequest:
    task: DroidTask
    requester_id: Optional[int]
    prompt_message: Optional[discord.Message] = None
    future: Optional[asyncio.Future] = None
    params: Optional[dict] = None


pending_ask_user_requests: dict[int, PendingAskUserRequest] = {}

BACKGROUND_TASKS: dict[str, asyncio.Task] = {}
BOT_INIT_LOCK = asyncio.Lock()
bot_runtime_initialized = False
bot_ready_once = False
discord_health_degraded_since: Optional[datetime] = None
discord_recovery_lock = asyncio.Lock()

HEALTH_CHECK_INTERVAL_SECONDS = 30
HEALTH_CHECK_UNHEALTHY_AFTER_SECONDS = 90
HEALTH_CHECK_RECOVERY_COOLDOWN_SECONDS = 180
_last_health_recovery_at: Optional[datetime] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _seconds_since(moment: Optional[datetime]) -> float:
    if moment is None:
        return 0.0
    return (_utcnow() - moment).total_seconds()


def _discord_connection_is_healthy() -> bool:
    ws = getattr(bot, "ws", None)
    if not bot.is_ready() or bot.is_closed() or ws is None:
        return False

    try:
        ws_open = getattr(ws, "open", None)
        if isinstance(ws_open, bool):
            return ws_open
        if callable(ws_open):
            return bool(ws_open())

        socket = getattr(ws, "socket", None)
        if socket is not None:
            socket_closed = getattr(socket, "closed", None)
            if isinstance(socket_closed, bool):
                return not socket_closed
            if callable(socket_closed):
                return not bool(socket_closed())
    except Exception:
        logger.exception("❌ Discord 健康检查读取 websocket 状态失败")
        return False

    return False


def _track_background_task(name: str, task: asyncio.Task) -> asyncio.Task:
    existing = BACKGROUND_TASKS.get(name)
    if existing and not existing.done():
        return existing

    BACKGROUND_TASKS[name] = task

    def _cleanup(completed: asyncio.Task, task_name: str = name) -> None:
        current = BACKGROUND_TASKS.get(task_name)
        if current is completed:
            BACKGROUND_TASKS.pop(task_name, None)
        try:
            completed.result()
        except asyncio.CancelledError:
            logger.info("🛑 后台任务已取消: %s", task_name)
        except Exception:
            logger.exception("❌ 后台任务异常退出: %s", task_name)

    task.add_done_callback(_cleanup)
    return task


async def _ensure_runtime_initialized() -> None:
    global workspace_mgr, task_registry, bot_runtime_initialized

    async with BOT_INIT_LOCK:
        if bot_runtime_initialized:
            return

        workspace_mgr = WorkspaceManager(workspaces_dir=WORKSPACES_DIR)
        task_registry = TaskRegistry()

        if DROID_TRANSPORT == "sdk":
            logger.warning(
                "⚠️ DROID_TRANSPORT=sdk 已启用：仅新鲜非文件任务走 SDK，"
                "权限审批 / ask-user 通过 Discord 桥接处理；"
                "恢复会话和文件操作任务继续走 CLI。"
            )

        stale_active = task_registry.get_active_tasks()
        for t in stale_active:
            task_registry.update_status(t["thread_id"], STATUS_WAITING)
        if stale_active:
            logger.info("🔁 重置 %s 个遗留 active 任务为 waiting", len(stale_active))

        _track_background_task(
            "auto_cleanup_loop",
            bot.loop.create_task(workspace_mgr.auto_cleanup_loop(task_registry)),
        )
        for index in range(MAX_CONCURRENT):
            _track_background_task(
                f"task_worker_{index}",
                bot.loop.create_task(task_worker()),
            )
        _track_background_task(
            "discord_health_watchdog",
            bot.loop.create_task(discord_health_watchdog()),
        )

        bot_runtime_initialized = True
        logger.info("📁 系统初始化完成 (Workers: %s, 工作区: %s)", MAX_CONCURRENT, WORKSPACES_DIR)


async def _attempt_discord_recovery(reason: str) -> bool:
    global _last_health_recovery_at

    async with discord_recovery_lock:
        if bot.is_closed():
            logger.warning("⚠️ 跳过 Discord 自愈：bot 已关闭 (%s)", reason)
            return False

        if _last_health_recovery_at and _seconds_since(_last_health_recovery_at) < HEALTH_CHECK_RECOVERY_COOLDOWN_SECONDS:
            logger.warning(
                "⏳ 跳过 Discord 自愈：距上次恢复仅 %.0fs (%s)",
                _seconds_since(_last_health_recovery_at),
                reason,
            )
            return False

        ws = getattr(bot, "ws", None)
        if not _discord_connection_is_healthy():
            logger.warning("🔄 Discord 健康检查触发关闭连接，等待 discord.py 自动重连: %s", reason)
        else:
            logger.warning("🔄 Discord 健康检查主动关闭网关连接以触发重连: %s", reason)

        _last_health_recovery_at = _utcnow()
        try:
            await bot.close()
        except Exception:
            logger.exception("❌ Discord 自愈关闭客户端失败")
            return False

        logger.warning("♻️ Discord 客户端已关闭，准备由外层 supervisor 重新启动")
        return True


async def discord_health_watchdog():
    global discord_health_degraded_since

    logger.info(
        "🩺 Discord 健康检查已启动 (interval=%ss, unhealthy_after=%ss)",
        HEALTH_CHECK_INTERVAL_SECONDS,
        HEALTH_CHECK_UNHEALTHY_AFTER_SECONDS,
    )

    while not bot.is_closed():
        healthy = _discord_connection_is_healthy()
        if healthy:
            if discord_health_degraded_since is not None:
                logger.info(
                    "✅ Discord 连接恢复健康，累计降级 %.1fs",
                    _seconds_since(discord_health_degraded_since),
                )
                discord_health_degraded_since = None
            else:
                logger.debug("✅ Discord 健康检查正常")
        else:
            if discord_health_degraded_since is None:
                discord_health_degraded_since = _utcnow()
                logger.warning("⚠️ Discord 健康检查检测到连接降级，开始计时")
            else:
                degraded_for = _seconds_since(discord_health_degraded_since)
                logger.warning("⚠️ Discord 连接仍未就绪/已断开，已持续 %.1fs", degraded_for)
                if degraded_for >= HEALTH_CHECK_UNHEALTHY_AFTER_SECONDS:
                    recovered = await _attempt_discord_recovery(
                        f"unhealthy for {degraded_for:.1f}s"
                    )
                    if recovered:
                        return

        await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)


def _select_transport_for_task(parsed, session_id=None) -> tuple[str, Optional[str]]:
    """
    Select the Cerebro transport for a task using a conservative policy.

    Phase 3 policy (DROID_TRANSPORT=sdk):
      - Fresh, non-file-operation tasks  -> SDK (interactive + permission bridging)
      - Resumed sessions (session_id set) -> CLI (SDK load_session does not re-apply
        model/autonomy settings; CLI preserves existing session behaviour)
      - File-operation tasks               -> CLI (approval UX depends on CLI event shape)

    SDK resume is intentionally gated on CLI because load_session() restores a session
    with whatever model/effort was active when it was first initialised — not the
    model's currently configured value.  Unlocking SDK resume safely requires a
    separate SDK-side "re-initialise" or "override settings" API that does not yet
    exist, so we keep resume on the stable CLI path.
    """
    requested_transport = DROID_TRANSPORT
    if requested_transport != "sdk":
        return requested_transport, None

    if session_id:
        return "cli", "resume requires current CLI session behavior"

    if parsed.is_file_operation:
        return "cli", "file-operation tasks require current CLI approval behavior"

    return "sdk", None


def _task_is_running(task: DroidTask) -> bool:
    return task.is_running


def _normalize_permission_result(result: dict) -> dict:
    selected_option = result.get("selected_option") or result.get("selectedOption") or "cancel"
    return {"selected_option": str(selected_option)}


def _normalize_ask_user_result(result: dict) -> dict:
    return {
        "cancelled": bool(result.get("cancelled", False)),
        "answers": list(result.get("answers", [])),
    }


async def _request_sdk_permission(
    thread: discord.Thread,
    handler: DroidEventHandler,
    params: dict,
) -> dict:
    tool_uses = params.get("toolUses") or params.get("tool_uses") or []
    if not tool_uses:
        raise RuntimeError("SDK 权限请求缺少 toolUses，已安全终止。")

    primary_tool = tool_uses[0] or {}
    tool_use = primary_tool.get("toolUse") or primary_tool.get("tool_use") or {}
    tool_name = tool_use.get("name") or primary_tool.get("toolName") or "tool"
    tool_input = tool_use.get("input") or primary_tool.get("parameters") or {}
    command = ""
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command", ""))

    await handler.flush_output()
    await handler.dashboard.update(status="⚠️ 等待权限审批...")

    if command and handler.approval.is_high_risk(command):
        approved = await handler.approval.request_high_risk(tool_name, command)
        if not approved:
            await thread.send("🚫 **已拒绝该权限请求，任务将安全终止。**")
            return {"selected_option": "cancel"}
    elif command:
        await handler.approval.notify_moderate(tool_name, command)
    else:
        approved = await handler.approval.request_high_risk(
            tool_name,
            f"{tool_name} (no command details provided)",
        )
        if not approved:
            await thread.send("🚫 **已拒绝该权限请求，任务将安全终止。**")
            return {"selected_option": "cancel"}

    options = params.get("options") or []
    option_values = {
        str(option.get("value"))
        for option in options
        if isinstance(option, dict) and option.get("value")
    }
    if "proceed_once" not in option_values:
        await thread.send("⚠️ **权限请求缺少安全的单次授权选项，已拒绝执行。**")
        return {"selected_option": "cancel"}

    return {"selected_option": "proceed_once"}


async def _request_sdk_ask_user(
    thread: discord.Thread,
    task: DroidTask,
    requester_id: Optional[int],
    params: dict,
) -> dict:
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    questions = params.get("questions") or []
    if not questions:
        raise RuntimeError("SDK ask-user 请求缺少 questions，已安全终止。")

    lines = ["❓ **Droid 需要你的补充信息才能继续：**"]
    for question in questions:
        if not isinstance(question, dict):
            continue
        index = question.get("index", "?")
        text = question.get("question", "")
        topic = question.get("topic", "")
        suffix = f"（{topic}）" if topic else ""
        lines.append(f"{index}. {text}{suffix}")
        options = question.get("options") or []
        if options:
            lines.append(f"   可选项: {', '.join(str(opt) for opt in options[:10])}")
    if len(questions) > 1:
        lines.append("⚠️ 当前仅支持单问题直接回复；多问题请求将安全拒绝，请让 Droid 改为逐条提问。")
    else:
        lines.append("请直接在当前线程回复；回复后任务会继续。")

    prompt_message = await thread.send("\n".join(lines))
    if len(questions) > 1:
        return {"cancelled": True, "answers": []}

    pending_ask_user_requests[thread.id] = PendingAskUserRequest(
        task=task,
        requester_id=requester_id,
        prompt_message=prompt_message,
        future=future,
        params=params,
    )

    try:
        return _normalize_ask_user_result(await future)
    finally:
        current = pending_ask_user_requests.get(thread.id)
        if current and current.future is future:
            pending_ask_user_requests.pop(thread.id, None)


# ============================================================================
# Worker
# ============================================================================

async def task_worker():
    """后台任务消费者"""
    logger.info("👷 Worker 已就绪")
    while True:
        task_data = await task_queue.get()

        # 支持多种格式：
        # - 最新格式: (thread, ParsedTask, model, session_id, requester_id)
        # - 新格式: (thread, ParsedTask, model, session_id)
        # - 旧格式: (thread, ParsedTask, model)
        # - 更旧格式: (thread, prompt_str, model)
        thread = task_data[0]
        model = task_data[2]
        session_id = task_data[3] if len(task_data) > 3 else None
        requester_id = task_data[4] if len(task_data) > 4 else None

        if isinstance(task_data[1], str):
            prompt = task_data[1]
            parsed = parse_task_command(prompt)
        else:
            parsed = task_data[1]
            prompt = parsed.task

        try:
            await _execute(thread, prompt, model, parsed, session_id, requester_id)
        except Exception as e:
            logger.error(f"Worker 异常: {e}", exc_info=True)
        finally:
            task_queue.task_done()


async def _execute(thread: discord.Thread, prompt: str, model: str, parsed=None, session_id=None, requester_id: int = None):
    """核心任务执行"""
    # 解析任务参数
    if parsed is None:
        parsed = parse_task_command(prompt)

    dashboard = TaskDashboard(parsed.task)
    await dashboard.send_to(thread)

    # 在获取工作区之前先确定 session_id 并注册任务，确保即使工作区获取失败也有记录
    if session_id is None:
        if task_registry:
            session_id = task_registry.get_session_id(thread.id)
            if session_id:
                logger.info(f"🔄 复用已有 session_id: {session_id[:8]}... for thread {thread.id}")
            else:
                logger.info(f"🆕 新任务，session_id 将由 droid 分配 for thread {thread.id}")
        else:
            logger.info(f"🆕 新任务，session_id 将由 droid 分配 for thread {thread.id}")
    else:
        logger.info(f"🔄 使用传入的 session_id: {session_id[:8]}... for thread {thread.id}")

    # 在获取工作区之前先注册任务（用占位符），确保即使工作区获取失败也有 DB 记录
    if task_registry:
        task_registry.register_task(
            thread.id,
            "pending",
            parsed.task,
            model,
            task_type="git_clone" if parsed.repo else ("workspace" if parsed.workspace else "temp"),
            parsed_data={
                "repo": parsed.repo,
                "workspace": parsed.workspace,
                "is_file_operation": parsed.is_file_operation,
            },
            session_id=session_id
        )

    task = None

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

        # 工作区就绪后更新真实路径
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
                },
                session_id=session_id
            )

        # 记录任务信息
        workspace_info = format_task_preview(parsed)
        await thread.send(f"📋 **任务模式:** {workspace_info}")

        selected_transport, fallback_reason = _select_transport_for_task(parsed, session_id=session_id)
        if fallback_reason:
            logger.info(
                "↩️ Thread %s falling back to cli transport: %s (requested=%s)",
                thread.id,
                fallback_reason,
                DROID_TRANSPORT,
            )

        handler = DroidEventHandler(thread, dashboard, requester_id=requester_id)
        task = DroidTask(
            cwd=isolated_cwd,
            transport_name=selected_transport,
        )
        if selected_transport == "sdk":
            task.transport.interaction_bridge = InteractionBridge(
                request_permission=lambda params: _request_sdk_permission(thread, handler, params),
                ask_user=lambda params: _request_sdk_ask_user(thread, task, requester_id, params),
            )
        active_tasks[thread.id] = task

        # 主事件循环 - 处理 Droid 输出
        async for event in task.run(parsed.task, model=model, session_id=session_id):
            if not await handler.handle(event):
                break

        # 任务完成后，捕获 droid 返回的真实 session_id 并持久化
        if task.session_id:
            session_id = task.session_id
            logger.info(f"✅ 已从 droid 捕获 session_id: {session_id[:8]}... for thread {thread.id}")
            if task_registry:
                task_registry.set_session_id(thread.id, session_id)

        # 任务完成，更新状态为 waiting（等待用户继续输入）
        if task_registry:
            task_registry.update_status(thread.id, STATUS_WAITING)

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
            await thread.send("✨ **任务结束，等待继续输入...**")

    except Exception as e:
        logger.error(f"任务 {thread.id} 失败: {e}")
        if task_registry:
            # 保存 session_id 确保用户可继续对话重试
            task_registry.update_status(thread.id, STATUS_WAITING)
            if session_id:
                task_registry.set_session_id(thread.id, session_id)
        await dashboard.error(str(e)[:800])
        await thread.send(f"❌ **任务执行失败:** {str(e)[:1900]}")

    finally:
        pending_ask_user_requests.pop(thread.id, None)
        # 延迟清理活跃任务记录，防止过早清理导致用户连续输入无法走 send_input
        def _delayed_cleanup():
            if task is None:
                return
            current_task = active_tasks.get(thread.id)
            if current_task is task and not _task_is_running(current_task):
                active_tasks.pop(thread.id, None)
        
        bot.loop.call_later(30.0, _delayed_cleanup)


# ============================================================================
# 生命周期
# ============================================================================

@bot.event
async def on_ready():
    global bot_ready_once, discord_health_degraded_since

    logger.info("✅ 已连接: %s", bot.user)

    try:
        await _ensure_runtime_initialized()
    except Exception as e:
        logger.error(f"初始化失败: {e}")
        await bot.close()
        return

    if bot_ready_once:
        logger.info("🔁 Discord 会话已恢复，跳过重复后台初始化")
    else:
        bot_ready_once = True
        logger.info("🚀 Discord 首次 ready 完成")

    discord_health_degraded_since = None

    try:
        synced = await bot.tree.sync()
        logger.info(f"🔄 同步 {len(synced)} 命令")
    except Exception as e:
        logger.error(f"命令同步失败: {e}")


@bot.event
async def on_connect():
    logger.info("🔌 Discord 网关连接已建立")


@bot.event
async def on_disconnect():
    global discord_health_degraded_since
    if discord_health_degraded_since is None:
        discord_health_degraded_since = _utcnow()
    logger.warning("📴 Discord 网关连接断开，等待库内建重连")


@bot.event
async def on_resumed():
    global discord_health_degraded_since
    discord_health_degraded_since = None
    logger.info("🔄 Discord 会话已恢复 (RESUMED)")


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

    # 立即 defer，防止 Discord 3秒超时
    await interaction.response.defer(ephemeral=True)

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

    # 预览任务配置
    preview = format_task_preview(parsed)
    queue_pos = task_queue.qsize() + 1
    msg = f"✅ 任务已提交。\n{preview}"
    if queue_pos > 1:
        msg = f"⏳ 已入队，当前排第 {queue_pos} 位。\n{preview}"

    await interaction.followup.send(msg, ephemeral=True)
    await task_queue.put((thread, parsed, model, None, interaction.user.id))


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

    # 只清理磁盘上的工作区文件，不删除 DB 记录
    await workspace_mgr.cleanup_workspace(thread_id, registry=None)
    
    # 清除 session_id，下次发消息将以新 session 继续
    task_registry.clear_session_id(thread_id)
    # 重置状态为 waiting，确保 is_resumable 返回 True
    task_registry.update_status(thread_id, STATUS_WAITING)
    logger.info(f"🆕 Thread {thread_id} 上下文已重置，session_id 已清除")

    await interaction.response.send_message("🆕 上下文已重置，直接发送消息即可开启新会话。", ephemeral=True)


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
    user_input = message.content.strip()

    # 检查是否发送了结束命令
    if user_input.lower() in ["/end", "结束", "bye", "再见"]:
        if thread_id in active_tasks:
            task = active_tasks[thread_id]
            task.kill()
            active_tasks.pop(thread_id, None)
        if task_registry:
            task_registry.update_status(thread_id, STATUS_COMPLETED)
        await message.channel.send("👋 **会话已手动结束**")
        await message.add_reaction("✅")
        return

    # 活跃任务：进程仍在运行，提示用户等待
    if thread_id in active_tasks:
        task = active_tasks[thread_id]

        pending_ask = pending_ask_user_requests.get(thread_id)
        if pending_ask and pending_ask.task is task and task.is_running:
            if pending_ask.requester_id and message.author.id != pending_ask.requester_id:
                await message.channel.send("⚠️ 当前 ask-user 仅接受任务发起人的回复。")
                await message.add_reaction("⛔")
                return

            questions = (pending_ask.params or {}).get("questions") or []
            answers = []
            for question in questions:
                if not isinstance(question, dict):
                    continue
                answers.append({
                    "index": question.get("index", len(answers) + 1),
                    "question": question.get("question", ""),
                    "answer": user_input,
                })

            if not answers:
                answers = [{"index": 1, "question": "", "answer": user_input}]

            if pending_ask.prompt_message:
                try:
                    await pending_ask.prompt_message.reply("✅ 已收到你的补充信息，任务继续执行中。")
                except Exception:
                    await message.channel.send("✅ 已收到你的补充信息，任务继续执行中。")
            else:
                await message.channel.send("✅ 已收到你的补充信息，任务继续执行中。")

            if pending_ask.future and not pending_ask.future.done():
                pending_ask.future.set_result({"cancelled": False, "answers": answers})
            pending_ask_user_requests.pop(thread_id, None)
            await message.add_reaction("✅")
            return

        if not _task_is_running(task):
            # 进程已确认退出，从活跃队列移除并走下方的重建对话逻辑
            active_tasks.pop(thread_id, None)
        else:
            if message.attachments:
                f_list = []
                for att in message.attachments:
                    save_path = Path(task.cwd) / att.filename
                    await att.save(save_path)
                    f_list.append(att.filename)
                await message.channel.send(f"📎 文件已保存，任务完成后将可使用: {', '.join(f_list)}")
            else:
                await message.channel.send("⏳ 任务正在执行中，请等待完成后再继续输入。")
            await message.add_reaction("⏳")
            return

    # 没有活跃任务，但检查是否有可继续的任务（waiting 或 completed 状态）
    if task_registry and task_registry.is_resumable(thread_id):
        task_info = task_registry.get_task_by_thread(thread_id)
        parsed_data = task_info.get("parsed_data", {})
        session_id = task_registry.get_session_id(thread_id)

        if user_input:
            await message.channel.send("🔄 继续对话...")
            try:
                from .parser import ParsedTask, _detect_file_operation
                parsed = ParsedTask(
                    task=user_input,
                    repo=parsed_data.get("repo"),
                    workspace=parsed_data.get("workspace"),
                    is_file_operation=_detect_file_operation(user_input) or parsed_data.get("is_file_operation", False),
                )
                await task_queue.put((message.channel, parsed, task_info["model"], session_id, message.author.id))
                await message.add_reaction("📥")
            except discord.errors.HTTPException as e:
                if e.code in (40072, 40025):  # Archived or locked thread
                    await message.channel.send("⚠️ 此会话已归档/锁定，无法继续回复。请在频道使用 /task 开启新会话。")
                    task_registry.update_status(thread_id, STATUS_COMPLETED)
                else:
                    raise
            return

    await bot.process_commands(message)


# ============================================================================
# 入口
# ============================================================================

async def _run_bot_supervisor():
    restart_delay_seconds = 5

    while True:
        try:
            await bot.start(TOKEN)
        except KeyboardInterrupt:
            logger.info("🛑 收到退出信号，正在停止 Cerebro")
            raise
        except Exception:
            logger.exception("❌ Discord 客户端异常退出，%ss 后尝试重启", restart_delay_seconds)
            if bot.is_closed():
                bot.clear()
            await asyncio.sleep(restart_delay_seconds)
            continue

        if bot.is_closed():
            logger.warning("♻️ Discord 客户端已关闭，%ss 后由 supervisor 重启", restart_delay_seconds)
            bot.clear()
            await asyncio.sleep(restart_delay_seconds)
            continue

        logger.info("🛑 Discord 客户端已停止，supervisor 即将退出")
        return


def main():
    if not TOKEN:
        logger.error("❌ 未发现 DISCORD_BOT_TOKEN")
        return
    logger.info("🚀 启动 Cerebro 引擎…")
    try:
        asyncio.run(_run_bot_supervisor())
    except KeyboardInterrupt:
        logger.info("👋 Cerebro 已停止")


if __name__ == "__main__":
    main()
