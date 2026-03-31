"""
Discord UI/UX 交互层组件

提供动态不刷屏的控制面板。
"""

import asyncio
import logging

import aiohttp
import discord

logger = logging.getLogger(__name__)


def _short_model_name(model: str) -> str:
    """Derive short name from model string.
    
    Examples:
        "MiniMax-M2.7" → "MiniMax"
        "claude-opus-4-6" → "Claude-4.6"
        "custom:MiniMax-M2.7" → "MiniMax"
    """
    # Remove prefix like "custom:" if present
    if ":" in model:
        model = model.split(":", 1)[1]
    # Handle known patterns
    if model.lower().startswith("claude"):
        # claude-opus-4-6 → Claude-4.6
        parts = model.replace("claude-", "").split("-")
        if len(parts) >= 2:
            return f"Claude-{parts[0]}.{parts[1]}"
        return "Claude"
    if model.lower().startswith("gpt"):
        # gpt-4o → GPT-4o
        return model.replace("gpt-", "GPT-").title()
    # Default: take first segment before hyphen
    return model.split("-")[0]


# Status emoji mapping
_STATUS_EMOJI = {
    "初始化": "⚙️",
    "思考": "🧠",
    "思考/回复中": "🧠",
    "工具执行中": "🔍",
    "工具完成": "✅",
    "等待确认": "⏳",
    "等待用户确认": "⏳",
    "等待用户输入": "💬",
    "完成": "✅",
    "错误": "❌",
}


def _get_emoji(status: str) -> str:
    """Get emoji for status."""
    for key, emoji in _STATUS_EMOJI.items():
        if key in status:
            return emoji
    return "🤖"


class TaskDashboard:
    """
    动态状态面板：单行消息原地编辑更新，避免刷屏。
    
    格式: 🤖 [模型简称] | [状态emoji] [状态文字]
    """

    # Color constants
    COLOR_INIT = discord.Color.blue()
    COLOR_RUNNING = discord.Color.yellow()
    COLOR_DONE = discord.Color.green()
    COLOR_ERROR = discord.Color.red()

    def __init__(self, prompt: str, model: str = "MiniMax-M2.7"):
        self.message: discord.Message | None = None
        self.model = model
        self.model_short = _short_model_name(model)
        self.status = "初始化"
        self.tool_name: str | None = None
        self._color = self.COLOR_INIT

    async def send_to(self, target: discord.abc.Messageable) -> None:
        """发送初始状态消息到指定频道/线程"""
        content = self._build_content()
        try:
            self.message = await target.send(content)
        except (aiohttp.ClientConnectorError, ConnectionResetError, OSError) as e:
            logger.warning(f"TaskDashboard.send_to failed: {e}")
            self.message = None

    def _build_content(self) -> str:
        """Build the single-line status message."""
        emoji = _get_emoji(self.status)
        status_text = self.status
        if self.tool_name:
            status_text = f"{self.tool_name} {status_text}"
        return f"🤖 {self.model_short} | {emoji} {status_text}"

    async def update(self, status: str | None = None, tool_name: str | None = None) -> None:
        """Update status. If tool_name provided, show tool-specific status."""
        if status is not None:
            self.status = status
        if tool_name is not None:
            self.tool_name = tool_name
        
        self._color = self.COLOR_RUNNING
        if self.message:
            try:
                await self.message.edit(content=self._build_content())
            except (aiohttp.ClientConnectorError, ConnectionResetError, OSError) as e:
                logger.warning(f"TaskDashboard.update failed: {e}")

    async def complete(self, final_message: str | None = None) -> None:
        """Show completion state."""
        self.status = final_message if final_message else "✅ 任务完成"
        self.tool_name = None
        self._color = self.COLOR_DONE
        if self.message:
            try:
                await self.message.edit(content=self._build_content())
            except (aiohttp.ClientConnectorError, ConnectionResetError, OSError) as e:
                logger.warning(f"TaskDashboard.complete failed: {e}")

    async def error(self, error_message: str) -> None:
        """Show error state."""
        self.status = f"❌ {error_message[:100]}"
        self.tool_name = None
        self._color = self.COLOR_ERROR
        if self.message:
            try:
                await self.message.edit(content=self._build_content())
            except (aiohttp.ClientConnectorError, ConnectionResetError, OSError) as e:
                logger.warning(f"TaskDashboard.error failed: {e}")
