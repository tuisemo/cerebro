"""
任务指令解析器

解析 /task 命令中的参数，支持以下格式：
/task <描述> repo:<仓库路径> workspace:<工作目录>
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedTask:
    """解析后的任务对象"""
    task: str                           # 清理后的任务描述
    repo: Optional[str] = None           # 指定的 Git 仓库路径
    workspace: Optional[str] = None      # 指定的工作目录
    is_file_operation: bool = False      # 是否需要文件系统


# 可能涉及文件操作的关键词
FILE_OPERATION_KEYWORDS = [
    "写", "创建", "修改", "删除", "保存", "文件", "编辑",
    "新建", "生成", "写入", "read", "write", "create", "edit", 
    "delete", "file", "save", "generate", "modify",
]

# Git 操作关键词（暗示需要 Git 仓库）
GIT_OPERATION_KEYWORDS = [
    "commit", "push", "pull", "merge", "branch", "checkout",
    "提交", "推送", "拉取", "合并", "分支", "rebase", "clone",
]


def parse_task_command(prompt: str) -> ParsedTask:
    """
    解析任务指令

    Args:
        prompt: 原始指令，如 "重构 login 模块 repo: D:/Projects/MyApp"

    Returns:
        ParsedTask 对象
    """
    result = ParsedTask(task=prompt)

    # 提取 repo:xxx
    repo_match = re.search(r'repo:\s*["\']?([^"\'\s]+)["\']?', prompt, re.IGNORECASE)
    if repo_match:
        result.repo = repo_match.group(1).strip()
        result.task = re.sub(r'repo:\s*["\']?[^"\'\s]+["\']?', '', result.task, flags=re.IGNORECASE).strip()

    # 提取 workspace:xxx
    workspace_match = re.search(r'workspace:\s*["\']?([^"\'\s]+)["\']?', prompt, re.IGNORECASE)
    if workspace_match:
        result.workspace = workspace_match.group(1).strip()
        result.task = re.sub(r'workspace:\s*["\']?[^"\'\s]+["\']?', '', result.task, flags=re.IGNORECASE).strip()

    # 判断是否需要文件系统操作
    result.is_file_operation = _detect_file_operation(result.task)

    return result


def _detect_file_operation(task: str) -> bool:
    """检测任务是否涉及文件操作"""
    task_lower = task.lower()
    
    # 检查文件操作关键词
    for keyword in FILE_OPERATION_KEYWORDS:
        if keyword.lower() in task_lower:
            return True
    
    # 检查 Git 操作关键词
    for keyword in GIT_OPERATION_KEYWORDS:
        if keyword.lower() in task_lower:
            return True
    
    return False


def format_task_preview(parsed: ParsedTask) -> str:
    """格式化任务预览"""
    parts = []
    
    if parsed.repo:
        parts.append(f"仓库: `{parsed.repo}`")
    if parsed.workspace:
        parts.append(f"工作区: `{parsed.workspace}`")
    if parsed.is_file_operation:
        parts.append("📁 文件模式")
    else:
        parts.append("💬 问答模式")
    
    return " | ".join(parts)
