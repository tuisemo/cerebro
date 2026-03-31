"""
Discord 消息速率限制器 (Rate Limiter)

确保向同一个 Channel/Thread 发送消息时，遵循最小时间间隔，防止触发 Discord 429 错误。
"""

import asyncio
import logging
import time
from typing import Union
import aiohttp
import discord

logger = logging.getLogger(__name__)


async def _send_with_retry(coro_func, *args, max_retries=3, base_delay=0.5, **kwargs):
    """Send a message with exponential backoff retry on connection errors."""
    last_error = None
    for attempt in range(max_retries):
        try:
            return await coro_func(*args, **kwargs)
        except (aiohttp.ClientConnectorError, ConnectionResetError, OSError) as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Discord send failed (attempt {attempt+1}/{max_retries}), retrying in {delay}s: {e}")
                await asyncio.sleep(delay)
            else:
                logger.error(f"Discord send failed after {max_retries} attempts: {e}")
    raise last_error


class MessageThrottle:
    """Discord 消息节流器"""

    MIN_INTERVAL = 1.0

    def __init__(self, channel: Union[discord.Thread, discord.TextChannel, discord.abc.Messageable]):
        self.channel = channel
        self._last_send_time = 0.0
        self._lock = asyncio.Lock()

    async def send(self, content: str, **kwargs):
        """发送消息，如果发送过于频繁则自动等待"""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_send_time

            if elapsed < self.MIN_INTERVAL:
                await asyncio.sleep(self.MIN_INTERVAL - elapsed)

            msg = await _send_with_retry(
                self.channel.send, content[:2000], **kwargs
            )
            self._last_send_time = time.monotonic()
            return msg

    async def edit(self, message: discord.Message, content: str, **kwargs):
        """编辑消息，同样遵循节流逻辑"""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_send_time

            if elapsed < self.MIN_INTERVAL:
                await asyncio.sleep(self.MIN_INTERVAL - elapsed)

            await _send_with_retry(
                message.edit, content=content[:2000], **kwargs
            )
            self._last_send_time = time.monotonic()
