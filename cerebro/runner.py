"""
Droid 进程控制与事件流解析模块
"""

import asyncio
import contextlib
import json
import logging
import os
import platform
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import AsyncGenerator, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_thread_pool = ThreadPoolExecutor(max_workers=8)
SUPPORTED_DROID_TRANSPORTS = {"cli", "sdk"}


class DroidProcessError(Exception):
    pass


class DroidTransportError(DroidProcessError):
    pass


@dataclass
class InteractionBridge:
    request_permission: Callable[[dict], Awaitable[dict]]
    ask_user: Callable[[dict], Awaitable[dict]]


class BaseDroidTransport:
    def __init__(self, cwd: str, interaction_bridge: Optional[InteractionBridge] = None):
        self.cwd = cwd
        self.process: Optional[subprocess.Popen] = None
        self.session_id: Optional[str] = None
        self.is_running: bool = False
        self.interaction_bridge = interaction_bridge

    async def run(
        self,
        prompt: str,
        model: str = "claude-3-5-sonnet-20241022",
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        raise NotImplementedError

    def kill(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass
        self.process = None


class CliDroidTransport(BaseDroidTransport):
    def __init__(self, cwd: str, interaction_bridge: Optional[InteractionBridge] = None):
        super().__init__(cwd, interaction_bridge=interaction_bridge)
        self.droid_exe = shutil.which("droid")
        if not self.droid_exe:
            raise FileNotFoundError("Droid CLI 未在环境变量中找到，请确认已安装 Droid。")

    async def run(
        self,
        prompt: str,
        model: str = "claude-3-5-sonnet-20241022",
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        import sys

        if sys.platform == "win32":
            prompt = f"""{prompt}

[System Note: You are operating in a Native Windows environment. Use PowerShell/CMD syntax
(e.g., 'dir', 'del', 'type') instead of Linux commands like 'rm -rf', 'ls', 'cat'.
File paths use backslashes on Windows.]"""

        cmd = [
            self.droid_exe,
            "exec",
            "--output-format", "debug",
            "--auto", "medium",
            "--cwd", self.cwd,
            "-m", model,
        ]
        if session_id:
            cmd.extend(["--session-id", session_id])
        cmd.append(prompt)

        logger.info(f"[RUNNER] Spawning: {cmd}")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _run_process():
            """在独立线程中运行整个进程，完全脱离 ProactorEventLoop 的 IOCP 管理"""
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=self.cwd,
                )
                self.process = proc
                count = 0
                for line_bytes in iter(proc.stdout.readline, b""):
                    if line_bytes:
                        count += 1
                        loop.call_soon_threadsafe(queue.put_nowait, line_bytes)
                proc.stdout.close()
                proc.wait()
                logger.info(f"[READER] done rc={proc.returncode} lines={count}")
            except Exception as e:
                logger.error(f"[RUNNER] thread error: {e}")
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    json.dumps({"type": "error", "text": str(e)}).encode(),
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        _thread_pool.submit(_run_process)
        self.is_running = True

        try:
            while True:
                try:
                    line_bytes = await asyncio.wait_for(queue.get(), timeout=120.0)
                except asyncio.TimeoutError:
                    logger.warning("[RUNNER] 120s timeout waiting for output")
                    yield {"type": "error", "text": "任务超时（120秒无输出）"}
                    break

                if line_bytes is None:
                    break

                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    event = json.loads(line_str)
                    logger.info(f"[EVENT] type={event.get('type', 'unknown')} keys={list(event.keys())}")
                    if event.get("type") == "system" and event.get("subtype") == "init" and event.get("session_id"):
                        self.session_id = event["session_id"]
                        logger.info(f"[RUNNER] Captured droid session_id: {self.session_id[:8]}...")
                    yield event
                except json.JSONDecodeError:
                    logger.info(f"[RAW] {line_str[:200]}")
                    yield {"type": "raw_output", "text": line_str}

        except Exception as e:
            logger.error(f"[RUNNER] consumer exception: {e}")
            yield {"type": "error", "text": str(e)}

        finally:
            self.is_running = False
            self.kill()


class SdkDroidTransport(BaseDroidTransport):
    def __init__(self, cwd: str, interaction_bridge: Optional[InteractionBridge] = None):
        super().__init__(cwd, interaction_bridge=interaction_bridge)
        self._client = None
        self._runner_task: Optional[asyncio.Task] = None
        self._cancel_requested: bool = False

    async def _import_sdk(self):
        try:
            from droid_sdk import (
                AssistantTextDelta,
                DroidClient,
                ErrorEvent,
                ProcessTransport,
                ThinkingTextDelta,
                ToolConfirmationOutcome,
                ToolProgress,
                ToolResult,
                ToolUse,
                TurnComplete,
                WorkingStateChanged,
            )
            from droid_sdk.schemas.enums import (
                AutonomyLevel,
                DroidInteractionMode,
                ReasoningEffort,
            )
        except Exception as exc:
            raise DroidTransportError(
                "Droid transport 'sdk' 不可用：缺少可导入的 droid-sdk 依赖。"
            ) from exc

        return {
            "AssistantTextDelta": AssistantTextDelta,
            "DroidClient": DroidClient,
            "ErrorEvent": ErrorEvent,
            "ProcessTransport": ProcessTransport,
            "ThinkingTextDelta": ThinkingTextDelta,
            "ToolConfirmationOutcome": ToolConfirmationOutcome,
            "ToolProgress": ToolProgress,
            "ToolResult": ToolResult,
            "ToolUse": ToolUse,
            "TurnComplete": TurnComplete,
            "WorkingStateChanged": WorkingStateChanged,
            "AutonomyLevel": AutonomyLevel,
            "DroidInteractionMode": DroidInteractionMode,
            "ReasoningEffort": ReasoningEffort,
        }

    def _build_prompt(self, prompt: str) -> str:
        import sys

        if sys.platform == "win32":
            return f"""{prompt}

[System Note: You are operating in a Native Windows environment. Use PowerShell/CMD syntax
(e.g., 'dir', 'del', 'type') instead of Linux commands like 'rm -rf', 'ls', 'cat'.
File paths use backslashes on Windows.]"""
        return prompt

    def _build_sdk_env(self) -> dict[str, str]:
        env = os.environ.copy()
        transport_override = env.get("DROID_TRANSPORT")
        if transport_override:
            env["CEREBRO_DROID_TRANSPORT"] = transport_override
            env.pop("DROID_TRANSPORT", None)
        return env

    @staticmethod
    def _machine_id() -> str:
        return f"cerebro-{platform.node() or 'host'}"

    async def _request_permission(self, params: dict) -> str:
        if not self.interaction_bridge:
            raise DroidTransportError(
                "SDK transport 缺少 Cerebro 交互桥接，无法安全完成权限审批。"
            )

        result = await self.interaction_bridge.request_permission(params)
        selected_option = result.get("selected_option") or result.get("selectedOption")
        if not selected_option:
            raise DroidTransportError("SDK 权限审批未返回有效结果，已安全终止。")
        return selected_option

    async def _ask_user(self, params: dict) -> dict:
        if not self.interaction_bridge:
            raise DroidTransportError(
                "SDK transport 缺少 Cerebro 交互桥接，无法完成 ask-user 交互。"
            )

        result = await self.interaction_bridge.ask_user(params)
        if not isinstance(result, dict):
            raise DroidTransportError("SDK ask-user 返回格式无效，已安全终止。")
        if "answers" not in result:
            raise DroidTransportError("SDK ask-user 缺少 answers 字段，已安全终止。")
        return result

    def _map_sdk_event(self, message, sdk: dict) -> list[dict]:
        if isinstance(message, sdk["AssistantTextDelta"]):
            return [{"type": "assistant_chunk", "text": message.text}]
        if isinstance(message, sdk["ThinkingTextDelta"]):
            return [{"type": "thinking", "text": message.text}]
        if isinstance(message, sdk["ToolUse"]):
            return [{
                "type": "tool_call",
                "toolId": message.tool_use_id,
                "toolName": message.tool_name,
                "parameters": message.tool_input,
            }]
        if isinstance(message, sdk["ToolResult"]):
            return [{
                "type": "tool_result",
                "toolId": "",
                "toolName": message.tool_name or "tool",
                "result": message.content,
                "isError": message.is_error,
            }]
        if isinstance(message, sdk["ToolProgress"]):
            return [{
                "type": "tool_result",
                "toolId": "",
                "toolName": message.tool_name,
                "result": message.content,
                "isError": False,
            }]
        if isinstance(message, sdk["ErrorEvent"]):
            return [{
                "type": "error",
                "text": f"{message.error_type}: {message.message}",
            }]
        if isinstance(message, sdk["TurnComplete"]):
            return [{"type": "completion"}]
        if isinstance(message, sdk["WorkingStateChanged"]):
            return []
        return []

    async def run(
        self,
        prompt: str,
        model: str = "claude-3-5-sonnet-20241022",
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        sdk = await self._import_sdk()

        transport = sdk["ProcessTransport"](
            exec_path=shutil.which("droid") or "droid",
            cwd=self.cwd,
            env=self._build_sdk_env(),
        )

        client = sdk["DroidClient"](transport=transport)
        self._client = client

        try:
            self.is_running = True
            self._cancel_requested = False
            self._runner_task = asyncio.current_task()
            async with client:
                client.set_permission_handler(self._request_permission)
                client.set_ask_user_handler(self._ask_user)

                prompt = self._build_prompt(prompt)

                if session_id:
                    # Resume path: load_session() restores a session with whatever
                    # model / autonomy was active when it was first created.
                    # Phase 3 keeps all resumed sessions on CLI so the model
                    # stays consistent with what the app configured.
                    result = await client.load_session(session_id=session_id)
                    self.session_id = session_id
                else:
                    result = await client.initialize_session(
                        machine_id=self._machine_id(),
                        cwd=self.cwd,
                        model_id=model,
                        interaction_mode=sdk["DroidInteractionMode"].Auto,
                        autonomy_level=sdk["AutonomyLevel"].Medium,
                        reasoning_effort=sdk["ReasoningEffort"].Medium,
                    )
                    self.session_id = result.session_id

                if not self.session_id and client.session_id:
                    self.session_id = client.session_id

                yield {
                    "type": "system",
                    "subtype": "init",
                    "session_id": self.session_id,
                    "transport": "sdk",
                }

                await client.add_user_message(text=prompt)

                async for message in client.receive_response():
                    if client.session_id and not self.session_id:
                        self.session_id = client.session_id
                    for event in self._map_sdk_event(message, sdk):
                        yield event

        except DroidTransportError:
            raise
        except Exception as exc:
            logger.error("[RUNNER] sdk transport exception: %s", exc, exc_info=True)
            yield {"type": "error", "text": str(exc)}
        finally:
            self.is_running = False
            self._runner_task = None
            self._client = None
            self.process = None

    def kill(self) -> None:
        self._cancel_requested = True
        self.is_running = False
        client = self._client
        runner_task = self._runner_task

        if client is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                async def _interrupt_and_close() -> None:
                    with contextlib.suppress(Exception):
                        if client.session_id:
                            await client.interrupt_session()
                    with contextlib.suppress(Exception):
                        await client.close()

                loop.create_task(_interrupt_and_close())

        if runner_task and not runner_task.done():
            runner_task.cancel()

        self.process = None


def get_droid_transport_name() -> str:
    return normalize_droid_transport_name(os.getenv("DROID_TRANSPORT", "cli"), source="DROID_TRANSPORT")


def normalize_droid_transport_name(transport_name: Optional[str], source: str = "transport") -> str:
    normalized_transport = (transport_name or "cli").strip().lower() or "cli"
    if normalized_transport not in SUPPORTED_DROID_TRANSPORTS:
        logger.warning(
            "Invalid %s=%r; falling back to 'cli'. Supported values: %s",
            source,
            transport_name,
            ", ".join(sorted(SUPPORTED_DROID_TRANSPORTS)),
        )
        return "cli"
    return normalized_transport


def create_droid_transport(
    transport_name: str,
    cwd: str,
    interaction_bridge: Optional[InteractionBridge] = None,
) -> BaseDroidTransport:
    normalized_transport = normalize_droid_transport_name(transport_name)
    if normalized_transport == "cli":
        return CliDroidTransport(cwd=cwd, interaction_bridge=interaction_bridge)
    if normalized_transport == "sdk":
        return SdkDroidTransport(cwd=cwd, interaction_bridge=interaction_bridge)
    raise DroidTransportError(
        f"未知的 Droid transport: {normalized_transport!r}。支持的值: 'cli', 'sdk'."
    )


class DroidTask:
    def __init__(
        self,
        cwd: str,
        transport_name: Optional[str] = None,
        interaction_bridge: Optional[InteractionBridge] = None,
    ):
        self.cwd = cwd
        self.transport_name = (
            get_droid_transport_name()
            if transport_name is None
            else normalize_droid_transport_name(transport_name)
        )
        self.transport = create_droid_transport(
            self.transport_name,
            cwd,
            interaction_bridge=interaction_bridge,
        )

    async def run(
        self,
        prompt: str,
        model: str = "claude-3-5-sonnet-20241022",
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        async for event in self.transport.run(prompt, model=model, session_id=session_id):
            self.session_id = self.transport.session_id
            yield event
        self.session_id = self.transport.session_id

    @property
    def process(self) -> Optional[subprocess.Popen]:
        return self.transport.process

    @property
    def is_running(self) -> bool:
        return self.transport.is_running

    def kill(self) -> None:
        self.transport.kill()
