"""
工作区管理器

支持三种工作区模式：
1. Repo 模式 - 克隆指定 Git 仓库
2. Workspace 模式 - 使用指定目录
3. Temp 模式 - 创建临时工作区
"""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class WorkspaceError(Exception):
    """工作区管理异常"""
    pass


class WorkspaceManager:
    """
    工作区管理器，支持多场景：
    - repo 模式：从指定 Git 仓库克隆
    - workspace 模式：直接使用指定目录
    - temp 模式：创建临时工作区
    """

    def __init__(self, workspaces_dir: str = "./droid_workspaces"):
        self.workspaces_dir = Path(workspaces_dir).resolve()
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    async def get_workspace(
        self,
        thread_id: int,
        repo_path: str = None,
        workspace_path: str = None,
        is_file_operation: bool = False,
    ) -> str:
        """
        获取或创建工作区

        Args:
            thread_id: Discord Thread ID
            repo_path: 指定 Git 仓库路径（可选）
            workspace_path: 指定工作目录路径（可选）
            is_file_operation: 是否需要文件系统操作

        Returns:
            工作区路径（始终返回有效路径）
        """
        # 模式1: 指定仓库 - 克隆 Git 仓库
        if repo_path:
            return await self._clone_repo(thread_id, repo_path)

        # 模式2: 指定目录 - 直接使用
        if workspace_path:
            return await self._use_directory(thread_id, workspace_path)

        # 模式3/4: 始终创建临时工作区（包括 QA 模式）
        return await self._create_temp_workspace(thread_id)

    async def _clone_repo(self, thread_id: int, repo_path: str) -> str:
        """从指定仓库克隆到工作区"""
        source_path = Path(repo_path).resolve()
        
        # 目标目录
        target_dir = self.workspaces_dir / str(thread_id)

        # 如果已存在，直接复用，不重新克隆
        if target_dir.exists():
            return str(target_dir)

        # 确保源路径存在
        if not source_path.exists():
            source_path.mkdir(parents=True, exist_ok=True)

        # 检查是否是 Git 仓库
        if (source_path / ".git").exists():
            # 是 Git 仓库，执行克隆
            result = await asyncio.create_subprocess_exec(
                "git", "clone", "--local", str(source_path), str(target_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await result.communicate()

            if result.returncode != 0:
                error_msg = stderr.decode("utf-8", errors="replace") if stderr else "未知错误"
                raise WorkspaceError(f"克隆仓库失败: {error_msg}")

            # 创建独立分支
            await asyncio.create_subprocess_exec(
                "git", "-C", str(target_dir), "checkout", "-b", f"droid-task-{thread_id}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            # 不是 Git 仓库，创建目录并初始化
            target_dir.mkdir(parents=True, exist_ok=True)
            result = await asyncio.create_subprocess_exec(
                "git", "init",
                cwd=str(target_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.communicate()

        return str(target_dir)

    async def _use_directory(self, thread_id: int, workspace_path: str) -> str:
        """直接使用指定目录"""
        target_path = Path(workspace_path).resolve()
        
        # 检查路径遍历：确保目标路径在允许的根目录内
        # 允许的根目录为 workspaces_dir 的父目录（允许 workspaces_dir 内的任意路径）
        allowed_root = self.workspaces_dir.parent.resolve()
        try:
            target_path.relative_to(allowed_root)
        except ValueError:
            raise WorkspaceError(f"路径遍历检测失败: {workspace_path} 超出允许根目录 {allowed_root}")
        
        target_path.mkdir(parents=True, exist_ok=True)
        
        return str(target_path)

    async def _create_temp_workspace(self, thread_id: int) -> str:
        """创建临时工作区"""
        target_dir = self.workspaces_dir / str(thread_id)
        
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            # 初始化为 Git 仓库（可选，便于后续生成 patch）
            await asyncio.create_subprocess_exec(
                "git", "init",
                cwd=str(target_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        
        return str(target_dir)

    async def get_existing_workspace(self, thread_id: int) -> Optional[str]:
        """获取已存在的工作区路径"""
        target_dir = self.workspaces_dir / str(thread_id)
        if target_dir.exists():
            return str(target_dir)
        return None

    async def generate_patch(self, thread_id: int) -> Optional[str]:
        """任务结束后，计算沙盒修改内容并生成 Patch 文件"""
        target_dir = self.workspaces_dir / str(thread_id)
        if not target_dir.exists():
            return None

        timeout = 10.0
        try:
            # 检查是否是 Git 仓库
            if not (target_dir / ".git").exists():
                return None

            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(target_dir), "add", ".",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                logging.warning(f"generate_patch: git add timed out for thread {thread_id}")
                return None

            status_proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(target_dir), "status", "--porcelain",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(status_proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                status_proc.kill()
                logging.warning(f"generate_patch: git status timed out for thread {thread_id}")
                return None
            status_output = stdout.decode("utf-8", errors="replace").strip()

            if status_output:
                commit_proc = await asyncio.create_subprocess_exec(
                    "git", "-C", str(target_dir), "commit", "-m", "Auto-commit by Droid",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(commit_proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    commit_proc.kill()
                    logging.warning(f"generate_patch: git commit timed out for thread {thread_id}")
                    return None

                # 获取当前分支与原始分支的 diff
                proc = await asyncio.create_subprocess_exec(
                    "git", "-C", str(target_dir), "diff", "HEAD~1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    logging.warning(f"generate_patch: git diff timed out for thread {thread_id}")
                    return None
                return stdout.decode("utf-8", errors="replace") if stdout else None

        except Exception as e:
            raise WorkspaceError(f"生成 Patch 失败: {e}")

    async def cleanup_workspace(self, thread_id: int, registry=None) -> None:
        """清理指定 Thread 的工作区"""
        target_dir = self.workspaces_dir / str(thread_id)
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)

        if registry:
            registry.delete_task(thread_id)

    async def auto_cleanup_loop(self, registry, interval_hours: int = 1):
        """后台定时清理任务：回收过期工作区"""
        while True:
            try:
                stale_list = registry.get_stale_workspaces(max_age_hours=24)
                for item in stale_list:
                    t_id = item["thread_id"]
                    await self.cleanup_workspace(t_id, registry=registry)
                logger.info(f"🧹 清理了 {len(stale_list)} 个过期工作区")
            except Exception as e:
                logger.warning(f"[auto_cleanup] 清理异常: {e}")

            await asyncio.sleep(interval_hours * 3600)
