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
        await self.throttle.send(f"⚡ `{tool_name}`: `{cmd[:200]}`")

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

    # Assistant text flush thresholds
    ASSISTANT_FLUSH_CHARS = 1200
    MAX_DISCORD_CHARS = 1950  # Discord limit minus safety margin

    def __init__(self, thread: discord.Thread, dashboard: TaskDashboard, requester_id: int = None):
        self.thread = thread
        self.dashboard = dashboard
        self._buffer = ""
        self._thinking_buffer = ""
        self._thinking_msg: discord.Message | None = None
        self._thinking_messages: list[discord.Message] = []  # Track all thinking messages for cleanup
        self._last_msg: discord.Message | None = None
        self.throttle = MessageThrottle(thread)
        self.approval = SmartApprovalHandler(thread, self.throttle, requester_id=requester_id)
        self._last_flush_time = time.time()

    def _chunk_text(self, text: str, max_chars: int = None) -> list[str]:
        """Split text into chunks at natural boundaries, no truncation.
        
        Strategy:
        1. Split on \\n\\n paragraphs first
        2. If any paragraph > max_chars, split on \\n lines
        3. If any line > max_chars, split on spaces (word wrap)
        4. Return list of chunks, each <= max_chars
        
        Args:
            text: The text to split
            max_chars: Maximum characters per chunk (default: MAX_DISCORD_CHARS)
        
        Returns:
            List of text chunks, each <= max_chars
        """
        if max_chars is None:
            max_chars = self.MAX_DISCORD_CHARS
        
        if len(text) <= max_chars:
            return [text] if text else []
        
        chunks = []
        
        # Step 1: Split on paragraph boundaries (\n\n)
        paragraphs = text.split("\n\n")
        
        for paragraph in paragraphs:
            if len(paragraph) <= max_chars:
                # Paragraph fits, add it
                chunks.append(paragraph)
            else:
                # Paragraph too big, try splitting on lines
                lines = paragraph.split("\n")
                current_chunk = ""
                
                for line in lines:
                    if not line.strip():
                        # Empty line - could be paragraph separator within a chunk
                        if current_chunk and len(current_chunk) + 2 <= max_chars:
                            current_chunk += "\n\n"
                        continue
                    
                    line_len = len(line)
                    
                    if line_len <= max_chars:
                        # Line fits, add it
                        if current_chunk:
                            # Add newline if we have content
                            if len(current_chunk) + 1 + line_len <= max_chars:
                                current_chunk += "\n" + line
                            else:
                                chunks.append(current_chunk)
                                current_chunk = line
                        else:
                            current_chunk = line
                    else:
                        # Line too long - split on spaces (word wrap)
                        words = line.split(" ")
                        if current_chunk:
                            chunks.append(current_chunk)
                            current_chunk = ""
                        
                        for word in words:
                            word_len = len(word)
                            if not word:
                                continue
                            if word_len <= max_chars:
                                if not current_chunk:
                                    current_chunk = word
                                elif len(current_chunk) + 1 + word_len <= max_chars:
                                    current_chunk += " " + word
                                else:
                                    chunks.append(current_chunk)
                                    current_chunk = word
                            else:
                                # Single word longer than max_chars - force split
                                if current_chunk:
                                    chunks.append(current_chunk)
                                    current_chunk = ""
                                # Split long word into chunks
                                while len(word) > max_chars:
                                    chunks.append(word[:max_chars])
                                    word = word[max_chars:]
                                if word:
                                    current_chunk = word
                
                if current_chunk:
                    chunks.append(current_chunk)
        
        # Merge tiny trailing chunks into previous chunks
        if len(chunks) > 1:
            merged_chunks = []
            for i, chunk in enumerate(chunks):
                if i > 0 and len(chunk) < 50 and merged_chunks:
                    # Tiny trailing chunk - try to append to previous
                    prev = merged_chunks[-1]
                    if len(prev) + 2 + len(chunk) <= max_chars:
                        merged_chunks[-1] = prev + "\n\n" + chunk
                        continue
                merged_chunks.append(chunk)
            chunks = merged_chunks
        
        # Final pass: ensure all chunks are within limit
        final_chunks = []
        for chunk in chunks:
            while len(chunk) > max_chars:
                # Split at last space before max_chars
                split_point = chunk.rfind(" ", 0, max_chars)
                if split_point <= 0:
                    # No space found, force split at max_chars
                    split_point = max_chars
                final_chunks.append(chunk[:split_point])
                chunk = chunk[split_point:].lstrip()
            if chunk:
                final_chunks.append(chunk)
        
        return final_chunks

    async def handle(self, event: dict) -> bool:
        """处理单个事件。返回 False 表示流应终止。"""
        etype = event.get("type", "")
        logger.info(f"[HANDLER] type={etype} keys={list(event.keys())}")

        if etype == "thinking":
            return await self._on_thinking(event)
        elif etype == "assistant_chunk":
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

    async def _on_thinking(self, event: dict) -> bool:
        """处理 thinking 事件 - 单独显示思考过程"""
        text = event.get("text", event.get("content", ""))
        if isinstance(text, dict):
            text = text.get("content", text.get("text", ""))
        text = str(text) if text else ""
        
        if text:
            # Append to thinking buffer (no cap - let it grow)
            self._thinking_buffer += text
            await self._thinking_flush()
            await self.dashboard.update(status="思考与回复中")
        return True

    async def _thinking_flush(self):
        """发送思考消息（支持多消息分页显示思考进度）"""
        if not self._thinking_buffer.strip():
            return

        # Calculate safe content size (header + code block wrapper overhead)
        header_base = "🤔 **Droid 思考中...**"
        code_wrapper = "\n```\n...\n```"
        header_overhead = len(header_base) + len(code_wrapper)
        safe_content = self.MAX_DISCORD_CHARS - header_overhead - 10  # Extra safety margin

        # Chunk the thinking content
        chunks = self._chunk_text(self._thinking_buffer, safe_content)
        total_chunks = len(chunks)

        try:
            if total_chunks == 1:
                # Single chunk that fits - send normally
                content = f"{header_base}\n```\n{chunks[0]}\n```"
                if self._thinking_msg is None:
                    self._thinking_msg = await self.throttle.send(content)
                else:
                    await self._thinking_msg.edit(content=content)
            else:
                # Multiple chunks - send as separate messages showing progress
                # First, clear old thinking messages if any
                if self._thinking_messages:
                    # Edit the first one to start fresh
                    pass
                
                # Send all chunks as separate messages (they stack in Discord)
                self._thinking_messages = []
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        header = f"🤔 **Droid 思考中...** (1/{total_chunks})"
                    else:
                        header = f"🤔 继续思考... ({i+1}/{total_chunks})"
                    content = f"{header}\n```\n{chunk}\n```"
                    msg = await self.throttle.send(content)
                    self._thinking_messages.append(msg)
                
                # Keep reference to last message for potential editing
                self._thinking_msg = self._thinking_messages[-1] if self._thinking_messages else None
                
        except Exception as e:
            logger.warning(f"[_thinking_flush] failed: {e}")
            self._thinking_msg = None
            self._thinking_messages = []

    async def _on_assistant_text(self, event: dict) -> bool:
        """处理 assistant_chunk 事件 - 使用智能 chunking"""
        # 兼容不同模型或事件类型的字段：text 或 content 或 delta
        text = event.get("text", event.get("content", event.get("delta", "")))
        if isinstance(text, dict):
            # 兼容嵌套的 openai 风格 Delta
            text = text.get("content", text.get("text", ""))
            
        text = str(text) if text else ""
        if text:
            self._buffer += text
            # Flush conditions: buffer reaches 1200 chars OR explicitly flushed
            # The _flush() method now uses intelligent chunking
            if len(self._buffer) >= self.ASSISTANT_FLUSH_CHARS:
                await self._flush()
                await self.dashboard.update(status="思考与回复中")
        return True

    async def _on_tool_call(self, event: dict) -> bool:
        await self._flush()
        tool_id = event.get("toolId", "unknown")
        tool_name = event.get("toolName", tool_id)

        if tool_name in ["Execute", "execute_command"]:
            cmd_detail = event.get("parameters", {}).get("command", "")
            cmd_safe = cmd_detail[:500].replace("```", "`` `")
            
            # 智能确认机制
            if self.approval.is_high_risk(cmd_detail):
                # 高危命令：阻塞等待确认
                await self.dashboard.update(status="⛔ 高危确认")
                approved = await self.approval.request_high_risk(tool_name, cmd_detail)
                if not approved:
                    return False
            elif tool_name in MODERATE_RISK_TOOLS:
                # 中等风险：通知后自动继续
                await self.approval.notify_moderate(tool_name, cmd_detail)
        elif tool_name in ["Create", "Edit", "write_file"]:
            file_path = event.get("parameters", {}).get("file_path", "文件")
            await self.dashboard.update(status="📝 修改中", tool_name=tool_name)
            return True

        await self.dashboard.update(status="🔧 执行中", tool_name=tool_name)
        return True

    async def _on_tool_result(self, event: dict) -> bool:
        tool_name = event.get("toolName", "tool")
        is_error = event.get("isError", False)
        raw_result = str(event.get("value", event.get("result", "")))

        if raw_result.strip():
            chunks = self._chunk_text(raw_result)
            for chunk in chunks:
                safe = chunk.replace("```", "`` `")[:1950]
                emoji = "❌" if is_error else "✅"
                await self.throttle.send(f"{emoji} `{tool_name}`: `{safe[:200]}`")

        await self.dashboard.update(status="✅ 完成", tool_name=tool_name)
        return True

    async def _on_completion(self, event: dict) -> bool:
        await self.flush_output()
        await self.dashboard.update(status="💬 继续输入")
        return False

    async def _on_error(self, event: dict) -> bool:
        await self.flush_output()
        error_msg = event.get("text", event.get("message", "未知错误"))
        await self.dashboard.error(error_msg[:800])
        return False

    async def _flush(self):
        """Flush buffer using intelligent chunking - no truncation"""
        if not self._buffer.strip():
            return

        # Use _chunk_text to split at natural boundaries
        chunks = self._chunk_text(self._buffer)
        
        logger.info(f"[FLUSH] sending {len(chunks)} chunks")

        try:
            for i, chunk in enumerate(chunks):
                self._last_msg = await self.throttle.send(chunk)
                logger.info(f"[FLUSH] sent chunk {i+1}/{len(chunks)} len={len(chunk)}")
        except Exception as e:
            logger.error(f"[_flush] send failed: {e}")
            self._last_msg = None

        self._buffer = ""
        self._last_flush_time = time.time()

    async def flush_output(self):
        """Final flush for any pending text, then clear thinking buffer"""
        await self._flush()
        # Clear thinking buffer - edit message to indicate done
        await self._clear_thinking()

    async def _clear_thinking(self):
        """Clear thinking state - edit last message to show completion.
        
        Note: We cannot delete Discord messages without special permissions,
        so multiple thinking messages will remain visible (showing progressive
        thinking output). The last one gets edited to show completion.
        """
        if self._thinking_buffer:
            # Edit the last thinking message to show completion
            if self._thinking_msg is not None:
                try:
                    # Use _chunk_text to get the final content (no truncation)
                    header = "🤔 **Droid 思考完成**"
                    code_wrapper = "\n```\n...\n```"
                    overhead = len(header) + len(code_wrapper) + 10
                    safe_content = self.MAX_DISCORD_CHARS - overhead
                    
                    chunks = self._chunk_text(self._thinking_buffer, safe_content)
                    if len(chunks) == 1:
                        formatted = f"{header}\n```\n{chunks[0]}\n```"
                    else:
                        # Multiple chunks - just show first chunk with indicator
                        formatted = f"{header} (内容较长，已分{len(chunks)}段显示)\n```\n{chunks[0]}\n```"
                    await self._thinking_msg.edit(content=formatted)
                except Exception as e:
                    logger.warning(f"[_clear_thinking] edit failed: {e}")
        
        # Clear all thinking state
        self._thinking_buffer = ""
        self._thinking_msg = None
        self._thinking_messages = []
