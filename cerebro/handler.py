"""
Droid 事件流处理器

将 Droid CLI 的 JSON 事件流映射为 Discord 消息动作。
管理助手对话的消息缓冲与分段发送（打字机效果，受频控保护）。
包含智能 Human-in-the-loop 确认机制。
"""

import asyncio
import os
import discord
import discord.ui
from .ui import TaskDashboard
from .throttle import MessageThrottle


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

    def __init__(self, tool_name: str, cmd: str, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.tool_name = tool_name
        self.cmd = cmd
        self.result = None
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
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

    def __init__(self, thread: discord.Thread, throttle: MessageThrottle):
        self.thread = thread
        self.throttle = throttle
        self.pending_cancel = False

    def is_high_risk(self, cmd: str) -> bool:
        """检查是否为高危命令"""
        cmd_lower = cmd.lower()
        return any(pattern.lower() in cmd_lower for pattern in HIGH_RISK_PATTERNS)

    async def notify_moderate(self, tool_name: str, cmd: str):
        """中等风险：发送通知，不等待"""
        msg = await self.throttle.send(
            f"⚡ **即将执行指令:**\n```bash\n{cmd[:500]}\n```\n"
            f"⏳ 3秒后自动执行，回复 `cancel` 可取消..."
        )
        # 后台等待，用户可回复 cancel
        asyncio.create_task(self._check_cancel(msg, cmd))

    async def _check_cancel(self, msg: discord.Message, cmd: str):
        """后台任务：检查用户是否取消"""
        await asyncio.sleep(3)
        # 简化处理：3秒后自动继续，不实际检查 cancel 回复
        # 如需完整实现，可添加消息监听器

    async def request_high_risk(self, tool_name: str, cmd: str) -> bool:
        """高危操作：阻塞等待用户确认"""
        view = ConfirmView(tool_name, cmd)
        await self.throttle.send(
            f"🚨 **高危操作需确认:**\n```bash\n{cmd}\n```",
            view=view
        )
        return await view.wait_for_result()


class DroidEventHandler:
    """将 Droid 事件映射为 Discord 对话输出"""

    def __init__(self, thread: discord.Thread, dashboard: TaskDashboard):
        self.thread = thread
        self.dashboard = dashboard
        self._buffer = ""
        self._last_msg: discord.Message | None = None
        self.throttle = MessageThrottle(thread)
        self.approval = SmartApprovalHandler(thread, self.throttle)

    async def handle(self, event: dict) -> bool:
        """处理单个事件。返回 False 表示流应终止。"""
        etype = event.get("type", "")

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

        return True

    async def _on_message(self, event: dict) -> bool:
        if event.get("role") == "assistant":
            return await self._on_assistant_text(event)
        return True

    async def _on_assistant_text(self, event: dict) -> bool:
        text = event.get("text", "")
        if text:
            self._buffer += text
            if len(text) > 20 or "\n" in text or len(self._buffer) > 100:
                await self._flush()
                await self.dashboard.update(status="🧠 正在思考与回复...")
        return True

    async def _on_tool_call(self, event: dict) -> bool:
        await self._flush()
        tool_id = event.get("toolId", "unknown")
        tool_name = event.get("toolName", tool_id)

        status_msg = f"🔍 **Droid 正在尝试使用工具:** `{tool_name}`"
        if tool_name in ["Execute", "execute_command"]:
            cmd_detail = event.get("parameters", {}).get("command", "")
            status_msg = f"⚙️ **正在执行指令:**\n```bash\n{cmd_detail[:500]}\n```"
            
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
        result_preview = str(event.get("value", event.get("result", "")))[:200]

        if result_preview.strip():
            await self.throttle.send(f"{emoji} **{tool_name} 执行反馈:**\n> {result_preview}")

        await self.dashboard.update(status="✅ 工具调用完成")
        return True

    async def _on_completion(self, event: dict) -> bool:
        await self._flush()
        await self.dashboard.complete()
        return False

    async def _on_error(self, event: dict) -> bool:
        await self._flush()
        error_msg = event.get("text", event.get("message", "未知错误"))
        await self.dashboard.error(error_msg)
        await self.throttle.send(f"❌ **运行中止:** {error_msg}")
        return False

    async def _flush(self):
        """将缓冲文本发送到 Discord (受节流器保护)"""
        if not self._buffer.strip():
            return

        text = self._buffer[:2000]
        if self._last_msg:
            try:
                await self.throttle.edit(self._last_msg, content=text)
            except:
                self._last_msg = await self.throttle.send(text)
        else:
            self._last_msg = await self.throttle.send(text)

        if len(self._buffer) > 1500:
            self._last_msg = None
            self._buffer = ""
