"""
Discord 消息速率限制器 (Rate Limiter)

确保向同一个 Channel/Thread 发送消息时，遵循最小时间间隔，防止触发 Discord 429 错误。
"""

import asyncio
import time
from typing import Union
import discord


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

            msg = await self.channel.send(content[:2000], **kwargs)
            self._last_send_time = time.monotonic()
            return msg

    async def edit(self, message: discord.Message, content: str, **kwargs):
        """编辑消息，同样遵循节流逻辑"""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_send_time

            if elapsed < self.MIN_INTERVAL:
                await asyncio.sleep(self.MIN_INTERVAL - elapsed)

            await message.edit(content=content[:2000], **kwargs)
            self._last_send_time = time.monotonic()
