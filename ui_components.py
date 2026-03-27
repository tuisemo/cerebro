"""
Discord UI/UX 交互层组件

提供动态不刷屏的控制面板，以及高危命令的人工拦截审批视图。
"""

import discord


class TaskDashboard:
    """
    动态状态面板：实时呈现任务进度与日志片段
    通过编辑同一条消息原地更新，避免刷屏
    """

    MAX_LOG_LENGTH = 800  # Discord Embed 限制，单个 Field 不超过 4096 字符

    def __init__(self, interaction: discord.Interaction, prompt: str):
        self.interaction = interaction
        self.message: discord.Message | None = None
        self.full_log = ""
        self.status = "初始化系统..."
        self.tool_name = "-"

        # 截断 prompt 避免 Embed 过长
        display_prompt = prompt[:150] + "..." if len(prompt) > 150 else prompt

        self.embed = discord.Embed(
            title="⚙️ Droid 任务运行中 (隔离沙盒)",
            description=f"**需求:** {display_prompt}",
            color=discord.Color.blue(),
        )
        self.embed.add_field(name="🔄 状态", value=self.status, inline=True)
        self.embed.add_field(name="🔧 当前工具", value=f"`{self.tool_name}`", inline=True)
        self.embed.set_footer(text="Droid Collaborator | Factory Droid x Discord")

    async def send(self) -> None:
        """发送初始面板消息"""
        await self.interaction.response.send_message(embed=self.embed)
        self.message = await self.interaction.original_response()

    async def update(
        self,
        status: str | None = None,
        tool_name: str | None = None,
        log_chunk: str | None = None,
    ) -> None:
        """
        更新面板状态

        Args:
            status: 新状态文本
            tool_name: 当前执行的工具名
            log_chunk: 新增的日志片段
        """
        if status is not None:
            self.status = status
            self.embed.set_field_at(0, name="🔄 状态", value=status, inline=True)

        if tool_name is not None:
            self.tool_name = tool_name
            self.embed.set_field_at(1, name="🔧 当前工具", value=f"`{tool_name}`", inline=True)

        if log_chunk is not None:
            self.full_log += log_chunk
            # 截断日志以避免超出 Discord 限制
            truncated_log = (
                self.full_log[-self.MAX_LOG_LENGTH:]
                if len(self.full_log) > self.MAX_LOG_LENGTH
                else self.full_log
            )
            log_text = f"**实时思考日志:**\n```text\n{truncated_log}\n```"

            # 重新构建 Embed 描述
            display_prompt = self.embed.description.split("**需求:** ")[-1].split("\n\n")[0]
            self.embed.description = f"{display_prompt}\n\n{log_text}"

        if self.message:
            await self.message.edit(embed=self.embed)

    async def complete(self, final_message: str | None = None) -> None:
        """
        标记任务完成

        Args:
            final_message: 最终状态消息
        """
        self.embed.color = discord.Color.green()
        status_text = final_message or "✅ 任务顺利完结"
        self.embed.set_field_at(0, name="🔄 状态", value=status_text, inline=True)
        self.embed.set_field_at(1, name="🔧 当前工具", value="-", inline=True)

        if self.message:
            await self.message.edit(embed=self.embed)

    async def error(self, error_message: str) -> None:
        """
        标记任务失败

        Args:
            error_message: 错误消息
        """
        self.embed.color = discord.Color.red()
        self.embed.set_field_at(0, name="🔄 状态", value=f"❌ 任务失败: {error_message}", inline=True)

        if self.message:
            await self.message.edit(embed=self.embed)


class ApprovalView(discord.ui.View):
    """
    高危命令拦截组件 (Human-in-the-loop)

    拦截文件修改、系统命令等高危工具调用，
    通过 Discord UI 按钮强制要求人工审批。
    """

    def __init__(self, task, tool_name: str, command_detail: str = ""):
        super().__init__(timeout=300)  # 5分钟未响应自动超时
        self.task = task
        self.tool_name = tool_name
        self.command_detail = command_detail
        self.approved = None

    @discord.ui.button(label="允许执行", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        """允许执行高危命令"""
        await self.task.send_input("y")
        await interaction.response.send_message(
            "✅ 权限已下发，继续执行",
            ephemeral=False,
        )
        self.approved = True
        self.stop()

    @discord.ui.button(label="拒绝执行", style=discord.ButtonStyle.danger, emoji="🚫")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        """拒绝执行高危命令"""
        await self.task.send_input("n")
        await interaction.response.send_message(
            "🚫 请求已驳回",
            ephemeral=False,
        )
        self.approved = False
        self.stop()

    @discord.ui.button(label="修改后执行", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def modify(self, interaction: discord.Interaction, button: discord.ui.Button):
        """请求修改命令后执行"""
        await interaction.response.send_message(
            "✏️ 请在回复中提供修改后的指令，Droid 将使用您的版本继续执行。",
            ephemeral=False,
        )
        self.approved = "modify"
        self.stop()


class ModelSelect(discord.ui.Select):
    """模型选择下拉菜单"""

    def __init__(self):
        options = [
            discord.SelectOption(
                label="Claude 3.5 Sonnet (推荐)",
                value="claude-3-5-sonnet-20241022",
                description="Anthropic 旗舰模型，代码能力最强",
                emoji="🤖",
            ),
            discord.SelectOption(
                label="GPT-4o",
                value="gpt-4o",
                description="OpenAI 最新多模态模型",
                emoji="🔷",
            ),
            discord.SelectOption(
                label="GPT-4o-mini",
                value="gpt-4o-mini",
                description="轻量级 OpenAI 模型",
                emoji="🔹",
            ),
        ]
        super().__init__(
            placeholder="选择 AI 模型...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        """用户选择模型后的回调"""
        self.view.model = self.values[0]
        await interaction.response.defer()
        self.view.stop()
