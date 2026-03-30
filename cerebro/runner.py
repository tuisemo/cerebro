"""
Droid 进程控制与事件流解析模块
"""

import asyncio
import json
import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator, Optional

logger = logging.getLogger(__name__)

_thread_pool = ThreadPoolExecutor(max_workers=8)


class DroidProcessError(Exception):
    pass


class DroidTask:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self.process: Optional[subprocess.Popen] = None
        self.session_id: Optional[str] = None
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
                    stdin=subprocess.DEVNULL,  # 不需要 stdin，直接接 devnull
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
                loop.call_soon_threadsafe(queue.put_nowait, json.dumps({"type": "error", "text": str(e)}).encode())
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        _thread_pool.submit(_run_process)

        process_completed_normally = False
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
                    # Capture droid's real session_id from system/init event
                    if event.get("type") == "system" and event.get("subtype") == "init" and event.get("session_id"):
                        self.session_id = event["session_id"]
                        logger.info(f"[RUNNER] Captured droid session_id: {self.session_id[:8]}...")
                    yield event
                    if event.get("type") == "completion":
                        process_completed_normally = True
                except json.JSONDecodeError:
                    logger.info(f"[RAW] {line_str[:200]}")
                    yield {"type": "raw_output", "text": line_str}

        except Exception as e:
            logger.error(f"[RUNNER] consumer exception: {e}")
            yield {"type": "error", "text": str(e)}

        finally:
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                except Exception:
                    pass
            self.process = None

    def kill(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass
            self.process = None
