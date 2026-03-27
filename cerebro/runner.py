"""
Droid 进程控制与事件流解析模块

负责底层 Droid CLI 进程的生命周期管理，双向数据流通信与 JSON 解析。
针对 Windows 原生环境进行了适配处理。
"""

import asyncio
import json
import shutil
from typing import AsyncGenerator, Optional


class DroidProcessError(Exception):
    """Droid 进程异常"""
    pass


class DroidTask:
    """Droid 进程任务管理器"""

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.process: Optional[asyncio.subprocess.Process] = None

        # [Windows 适配] 动态寻找可执行文件路径
        self.droid_exe = shutil.which("droid")
        if not self.droid_exe:
            raise FileNotFoundError(
                "Droid CLI 未在环境变量中找到，请确认已安装 Droid。"
            )

    async def run(
        self,
        prompt: str,
        model: str = "claude-3-5-sonnet-20241022",
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        """
        启动 Droid 进程并yield解析后的事件流

        Args:
            prompt: 用户输入的指令
            model: 使用的模型标识
            session_id: 可选的会话ID

        Yields:
            解析后的事件字典，包含 type, text, toolName 等字段
        """
        # [Windows 适配] 注入系统级 Prompt，规范大模型在 Windows 下的 Shell 行为
        windows_prompt = f"""{prompt}

[System Note: You are operating in a Native Windows environment. Use PowerShell/CMD syntax 
(e.g., 'dir', 'del', 'type') instead of Linux commands like 'rm -rf', 'ls', 'cat'. 
File paths use backslashes on Windows.]"""

        cmd = [
            self.droid_exe,
            "exec",
            "--output-format", "debug",
            "--auto", "medium",  # 自动确认中等风险操作
            "--cwd", self.cwd,
            "-m", model,
        ]
        if session_id:
            cmd.extend(["--session-id", session_id])
        cmd.append(windows_prompt)

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )

        buffer = ""
        try:
            async for line in self.process.stdout:
                # [Windows 适配] Windows 系统命令常输出 GBK/UTF-8 混杂编码
                buffer += line.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line_str, buffer = buffer.split("\n", 1)
                    line_str = line_str.strip()
                    if not line_str:
                        continue
                    try:
                        yield json.loads(line_str)
                    except json.JSONDecodeError:
                        # 非 JSON 行，可能是普通输出
                        yield {"type": "raw_output", "text": line_str}
        finally:
            if self.process:
                await self.process.wait()
                self.process = None

    async def send_input(self, text: str) -> None:
        """
        向 Droid 进程写入交互流（如批准 Y/N 或补充文字）

        Args:
            text: 要发送给进程的文本
        """
        if self.process and self.process.stdin:
            self.process.stdin.write(f"{text}\n".encode("utf-8"))
            await self.process.stdin.drain()

    def kill(self) -> None:
        """异常情况下强杀进程"""
        if self.process:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            self.process = None
