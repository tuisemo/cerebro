"""
Droid 事件流处理器

将 Droid CLI 的 JSON 事件流映射为 Discord 消息动作。
管理助手对话的消息缓冲与分段发送（打字机效果，受频控保护）。
包含智能 Human-in-the-loop 确认机制。
"""

import asyncio
import logging
import os
import discord
import discord.ui
import time
from .ui import TaskDashboard
from .throttle import MessageThrottle

logger = logging.getLogger(__name__)


# 高危命令模式（需用户显式确认）
HIGH_RISK_PATTERNS = [
    "rm -rf", "rm /", "del /", "del \\",
    "format ", "shutdown", "reboot",
    "--force --force",
]

# 中等风险工具（执行前通知，3秒后自动继续）
MODERATE_RISK_TOOLS = ["Execute", "execute_command"]


class ConfirmView(discord.ui.View):
    """高危操作确认按钮"""

    def __init__(self, tool_name: str, cmd: str, requester_id: int = None, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.tool_name = tool_name
        self.cmd = cmd
        self.requester_id = requester_id
        self.result = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.requester_id and interaction.user.id != self.requester_id:
            await interaction.response.send_message("⚠️ 只有任务发起人可以操作此确认按钮。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="允许执行", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        await interaction.response.edit_message(
            content=f"✅ **已授权执行:** `{self.tool_name}`",
            view=None
        )
        self.stop()

    @discord.ui.button(label="拒绝执行", style=discord.ButtonStyle.danger, emoji="🚫")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        await interaction.response.edit_message(
            content=f"🚫 **已拒绝:** `{self.tool_name}`",
            view=None
        )
        self.stop()

    async def wait_for_result(self) -> bool:
        """等待用户点击，返回 True=允许，False=拒绝"""
        await self.wait()
        return self.result is True


class SmartApprovalHandler:
    """
    智能确认机制 - 非阻塞式
    
    - 高危命令：阻塞等待用户确认
    - 中等风险：发送通知，3秒后自动继续
    - 普通操作：直接放行
    """

    def __init__(self, thread: discord.Thread, throttle: MessageThrottle, requester_id: int = None):
        self.thread = thread
        self.throttle = throttle
        self.requester_id = requester_id

    def is_high_risk(self, cmd: str) -> bool:
        """检查是否为高危命令"""
        cmd_lower = cmd.lower()
        return any(pattern.lower() in cmd_lower for pattern in HIGH_RISK_PATTERNS)

    async def notify_moderate(self, tool_name: str, cmd: str):
        """中等风险：发送通知，不等待"""
        await self.throttle.send(
            f"⚡ **即将执行指令:**\n```bash\n{cmd[:500]}\n```"
        )

    async def request_high_risk(self, tool_name: str, cmd: str) -> bool:
        """高危操作：阻塞等待用户确认"""
        view = ConfirmView(tool_name, cmd, requester_id=self.requester_id)
        await self.throttle.send(
            f"🚨 **高危操作需确认:**\n```bash\n{cmd}\n```",
            view=view
        )
        return await view.wait_for_result()


class DroidEventHandler:
    """将 Droid 事件映射为 Discord 对话输出"""

    def __init__(self, thread: discord.Thread, dashboard: TaskDashboard, requester_id: int = None):
        self.thread = thread
        self.dashboard = dashboard
        self._buffer = ""
        self._last_msg: discord.Message | None = None
        self.throttle = MessageThrottle(thread)
        self.approval = SmartApprovalHandler(thread, self.throttle, requester_id=requester_id)
        self._last_flush_time = time.time()

    async def handle(self, event: dict) -> bool:
        """处理单个事件。返回 False 表示流应终止。"""
        etype = event.get("type", "")
        logger.info(f"[HANDLER] type={etype} keys={list(event.keys())}")

        if etype in ("assistant_chunk", "thinking"):
            return await self._on_assistant_text(event)
        elif etype == "message":
            return await self._on_message(event)
        elif etype == "tool_call":
            return await self._on_tool_call(event)
        elif etype == "tool_result":
            return await self._on_tool_result(event)
        elif etype == "completion":
            return await self._on_completion(event)
        elif etype == "error":
            return await self._on_error(event)
            
        # 兜底逻辑：未知类型事件也尝试提取文本内容
        if "text" in event or "content" in event:
            text = event.get("text", event.get("content", ""))
            if text:
                event["text"] = str(text)
                return await self._on_assistant_text(event)

        return True

    async def _on_message(self, event: dict) -> bool:
        if event.get("role") == "assistant":
            return await self._on_assistant_text(event)
        return True

    async def _on_assistant_text(self, event: dict) -> bool:
        # 兼容不同模型或事件类型的字段：text 或 content 或 delta
        text = event.get("text", event.get("content", event.get("delta", "")))
        if isinstance(text, dict):
            # 兼容嵌套的 openai 风格 Delta
            text = text.get("content", text.get("text", ""))
            
        text = str(text) if text else ""
        if text:
            self._buffer += text
            current_time = time.time()
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[TEXT] buffer_len=%d text_len=%d will_flush=%s", len(self._buffer), len(text), len(text) > 10 or "\n" in text or len(self._buffer) > 50 or (current_time - self._last_flush_time) > 2.0)
            # 降低 flush 阈值：短消息或换行符触发，或 buffer 超过阈值，或超过时间阈值
            if len(text) > 10 or "\n" in text or len(self._buffer) > 50 or (current_time - self._last_flush_time) > 2.0:
                await self._flush()
                self._last_flush_time = current_time
                await self.dashboard.update(status="🧠 正在思考与回复...")
        return True

    async def _on_tool_call(self, event: dict) -> bool:
        await self._flush()
        tool_id = event.get("toolId", "unknown")
        tool_name = event.get("toolName", tool_id)

        status_msg = f"🔍 **Droid 正在尝试使用工具:** `{tool_name}`"
        if tool_name in ["Execute", "execute_command"]:
            cmd_detail = event.get("parameters", {}).get("command", "")
            cmd_safe = cmd_detail[:500].replace("```", "`` `")
            status_msg = f"⚙️ **正在执行指令:**\n```bash\n{cmd_safe}\n```"
            
            # 智能确认机制
            if self.approval.is_high_risk(cmd_detail):
                # 高危命令：阻塞等待确认
                await self.dashboard.update(status=f"⚠️ 等待高危操作确认...")
                approved = await self.approval.request_high_risk(tool_name, cmd_detail)
                if not approved:
                    await self.throttle.send(f"🚫 用户拒绝执行，任务终止")
                    return False
            elif tool_name in MODERATE_RISK_TOOLS:
                # 中等风险：通知后自动继续
                await self.approval.notify_moderate(tool_name, cmd_detail)
                
        elif tool_name in ["Create", "Edit", "write_file"]:
            file_path = event.get("parameters", {}).get("file_path", "文件")
            status_msg = f"📝 **正在修改文件:** `{os.path.basename(file_path)}`"

        await self.dashboard.update(status=f"⚙️ 执行中: {tool_name}")
        await self.throttle.send(status_msg)
        return True

    async def _on_tool_result(self, event: dict) -> bool:
        tool_id = event.get("toolId", "unknown")
        tool_name = event.get("toolName", tool_id)
        is_error = event.get("isError", False)

        emoji = "❌" if is_error else "✅"
        raw_result = str(event.get("value", event.get("result", "")))
        truncated = len(raw_result) > 1000
        result_preview = raw_result[:1000]

        if result_preview.strip():
            suffix = "\n> *(已截断，完整结果见工作区)*" if truncated else ""
            await self.throttle.send(f"{emoji} **{tool_name} 执行反馈:**\n```\n{result_preview}\n```{suffix}")

        await self.dashboard.update(status="✅ 工具调用完成")
        return True

    async def _on_completion(self, event: dict) -> bool:
        await self._flush()
        # 发送任务完成确认消息
        await self.throttle.send("✅ **任务执行完成**")
        await self.dashboard.update(status="⏳ 等待用户继续输入...")
        # 返回 False 让事件循环正常结束，进程会退出
        # 但 session 保持活跃状态，后续通过新进程继续对话
        return False

    async def _on_error(self, event: dict) -> bool:
        await self._flush()
        error_msg = event.get("text", event.get("message", "未知错误"))
        await self.dashboard.error(error_msg)
        await self.throttle.send(f"❌ **运行中止:** {error_msg}")
        return False

    async def _flush(self):
        """将缓冲文本发送到 Discord (受节流器保护)，支持超长内容分页"""
        if not self._buffer.strip():
            return

        chunk = self._buffer[:2000]
        remainder = self._buffer[2000:]
        logger.info(f"[FLUSH] sending len={len(chunk)} remainder={len(remainder)} has_last_msg={self._last_msg is not None}")

        send_success = False
        # 只有在没有后续内容时才尝试 edit（避免覆盖已发出的消息）
        if self._last_msg and not remainder:
            try:
                await self.throttle.edit(self._last_msg, content=chunk)
                send_success = True
            except Exception as e:
                logger.warning(f"[_flush] edit failed: {e}")
                self._last_msg = None

        if not send_success:
            try:
                self._last_msg = await self.throttle.send(chunk)
                send_success = True
            except Exception as e:
                logger.error(f"[_flush] send failed: {e}")

        if not send_success:
            return

        self._buffer = remainder
        self._last_flush_time = time.time()
        # 有剩余内容时强制下次发新消息，避免覆盖已发出的分页
        if remainder:
            self._last_msg = None
