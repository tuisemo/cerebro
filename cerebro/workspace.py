"""
Git 沙盒隔离管理器

负责在多个任务并发时，为每个 Discord Thread 提供独立的工作副本。
利用 Git --local 特性实现极速硬链接克隆，不占用额外磁盘空间。
"""

import asyncio
from pathlib import Path
from typing import Optional


class WorkspaceError(Exception):
    """工作区管理异常"""
    pass


class WorkspaceManager:
    """
    工作区管理器，为每个 Discord Thread 创建独立的 Git 沙盒
    """

    def __init__(self, base_repo_path: str, workspaces_dir: str = "./droid_workspaces"):
        self.base_repo = Path(base_repo_path).resolve()
        self.workspaces_dir = Path(workspaces_dir).resolve()
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

        if not self.base_repo.exists():
            raise WorkspaceError(f"基础仓库路径不存在: {self.base_repo}")

        if not (self.base_repo / ".git").exists():
            raise WorkspaceError(f"基础仓库不是有效的 Git 仓库: {self.base_repo}")

    async def get_or_create_workspace(self, thread_id: int) -> str:
        """为当前 Thread 创建独立的 Git 沙盒"""
        target_dir = self.workspaces_dir / str(thread_id)

        if not target_dir.exists():
            result = await asyncio.create_subprocess_exec(
                "git", "clone", "--local", str(self.base_repo), str(target_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await result.communicate()

            if result.returncode != 0:
                error_msg = stderr.decode("utf-8", errors="replace") if stderr else "未知错误"
                raise WorkspaceError(f"克隆仓库失败: {error_msg}")

            branch_result = await asyncio.create_subprocess_exec(
                "git", "-C", str(target_dir), "checkout", "-b", f"droid-task-{thread_id}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await branch_result.communicate()

        return str(target_dir)

    async def generate_patch(self, thread_id: int) -> Optional[str]:
        """任务结束后，计算沙盒修改内容并生成 Patch 文件"""
        target_dir = self.workspaces_dir / str(thread_id)
        if not target_dir.exists():
            return None

        try:
            await asyncio.create_subprocess_exec(
                "git", "-C", str(target_dir), "add", ".",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            status_proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(target_dir), "status", "--porcelain",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await status_proc.communicate()
            status_output = stdout.decode("utf-8", errors="replace").strip()

            if status_output:
                await asyncio.create_subprocess_exec(
                    "git", "-C", str(target_dir), "commit", "-m", "Auto-commit by Droid",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )

            branch_proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(target_dir), "rev-parse", "--abbrev-ref", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await branch_proc.communicate()
            current_branch = stdout.decode("utf-8", errors="replace").strip()

            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(target_dir), "format-patch", current_branch, "--stdout",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode("utf-8", errors="replace") if stdout else None

        except Exception as e:
            raise WorkspaceError(f"生成 Patch 失败: {e}")

    async def cleanup_workspace(self, thread_id: int, registry=None) -> None:
        """清理指定 Thread 的工作区，并同步清理数据库记录"""
        target_dir = self.workspaces_dir / str(thread_id)
        if target_dir.exists():
            import shutil
            shutil.rmtree(target_dir, ignore_errors=True)

        if registry:
            registry.delete_task(thread_id)

    async def auto_cleanup_loop(self, registry, interval_hours: int = 1):
        """后台定时清理任务：回收 24 小时前的旧任务沙盒"""
        while True:
            try:
                stale_list = registry.get_stale_workspaces(max_age_hours=24)
                for item in stale_list:
                    t_id = item["thread_id"]
                    await self.cleanup_workspace(t_id, registry=registry)

                await asyncio.sleep(interval_hours * 3600)
            except Exception as e:
                await asyncio.sleep(300)
