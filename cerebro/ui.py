"""
Discord UI/UX 交互层组件

提供动态不刷屏的控制面板。
"""

import discord


class TaskDashboard:
    """
    动态状态面板：实时呈现任务基础状态。
    通过编辑同一条消息原地更新，避免刷屏。
    """

    def __init__(self, prompt: str):
        self.message: discord.Message | None = None
        self.status = "初始化系统..."

        self.prompt_text = prompt[:150] + "..." if len(prompt) > 150 else prompt

        self.embed = discord.Embed(
            title="⚙️ Cerebro 运行中",
            description=f"**任务驱动:** {self.prompt_text}",
            color=discord.Color.blue(),
        )
        self.embed.add_field(name="🔄 状态", value=self.status, inline=True)
        self.embed.add_field(name="🕒 控制模式", value="无人值守(自动)", inline=True)
        self.embed.set_footer(text="Cerebro · 群体智能协同引擎")

    async def send_to(self, target: discord.abc.Messageable) -> None:
        """发送初始状态消息到指定频道/线程"""
        self.message = await target.send(embed=self.embed)

    async def update(self, status: str | None = None) -> None:
        if status is not None:
            self.status = status
            self.embed.set_field_at(0, name="🔄 状态", value=status, inline=True)

        if self.message:
            await self.message.edit(embed=self.embed)

    async def complete(self, final_message: str | None = None) -> None:
        self.embed.color = discord.Color.green()
        status_text = final_message or "✅ 任务顺利完结"
        self.embed.set_field_at(0, name="🔄 状态", value=status_text, inline=True)

        if self.message:
            await self.message.edit(embed=self.embed)

    async def error(self, error_message: str) -> None:
        self.embed.color = discord.Color.red()
        # Discord embed field 有 1024 字符限制，截断
        truncated_msg = error_message[:1000] + ("..." if len(error_message) > 1000 else "")
        self.embed.set_field_at(0, name="🔄 状态", value=f"❌ 任务失败: {truncated_msg}", inline=True)

        if self.message:
            await self.message.edit(embed=self.embed)
