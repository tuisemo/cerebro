"""
任务注册表 — 基于 SQLite 的轻量持久化层

用于在机器人重启、崩溃后保留任务元数据，并支持自动清理参考。
记录任务类型：git_clone | workspace | temp | qa
状态：active | completed | closed
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import List, Dict, Any, Optional


# 任务状态常量
STATUS_ACTIVE = "active"      # 执行中
STATUS_COMPLETED = "completed"  # 已完成，可继续
STATUS_CLOSED = "closed"       # 已关闭，不可继续


class TaskRegistry:
    def __init__(self, db_path: str = "./droid_tasks.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        """初始化数据库表结构"""
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        thread_id   INTEGER PRIMARY KEY,
                        workspace   TEXT NOT NULL,
                        prompt      TEXT,
                        model       TEXT,
                        task_type   TEXT DEFAULT 'unknown',
                        status      TEXT DEFAULT 'active',
                        parsed_data TEXT DEFAULT '{}',
                        last_update REAL NOT NULL
                    )
                """)
            except sqlite3.OperationalError:
                pass
            
            # 确保新列存在
            for col, default in [("task_type", "unknown"), ("parsed_data", "{}")]:
                try:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT DEFAULT '{default}'")
                except sqlite3.OperationalError:
                    pass
            
            conn.commit()

    def register_task(
        self, 
        thread_id: int, 
        workspace: str, 
        prompt: str = "", 
        model: str = "",
        task_type: str = "unknown",
        parsed_data: dict = None
    ):
        """记录新任务或更新现有任务"""
        parsed_json = json.dumps(parsed_data or {}, ensure_ascii=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO tasks (thread_id, workspace, prompt, model, task_type, status, parsed_data, last_update)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (thread_id, workspace, prompt, model, task_type, STATUS_ACTIVE, parsed_json, time.time()))
            conn.commit()

    def update_status(self, thread_id: int, status: str):
        """更新任务状态"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE tasks SET status = ?, last_update = ? WHERE thread_id = ?
            """, (status, time.time(), thread_id))
            conn.commit()

    def get_task_by_thread(self, thread_id: int) -> Optional[Dict[str, Any]]:
        """获取线程对应的任务信息"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("""
                SELECT thread_id, workspace, prompt, model, task_type, status, parsed_data
                FROM tasks WHERE thread_id = ?
            """, (thread_id,))
            row = cur.fetchone()
            if row:
                return {
                    "thread_id": row["thread_id"],
                    "workspace": row["workspace"],
                    "prompt": row["prompt"],
                    "model": row["model"],
                    "task_type": row["task_type"],
                    "status": row["status"],
                    "parsed_data": json.loads(row["parsed_data"] or "{}"),
                }
            return None

    def get_stale_workspaces(self, max_age_hours: int = 24) -> List[Dict[str, Any]]:
        """获取超过指定时间且非活跃的工作区列表"""
        cutoff = time.time() - (max_age_hours * 3600)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("""
                SELECT thread_id, workspace FROM tasks 
                WHERE (status != 'active' AND last_update < ?)
                OR (last_update < ?)
            """, (cutoff, time.time() - (48 * 3600)))
            return [{"thread_id": r[0], "workspace": r[1]} for r in cur.fetchall()]

    def delete_task(self, thread_id: int):
        """物理删除任务记录"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM tasks WHERE thread_id = ?", (thread_id,))
            conn.commit()

    def get_active_tasks(self) -> List[Dict[str, Any]]:
        """获取所有标记为 active 的任务"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT thread_id, workspace, model FROM tasks WHERE status = 'active'")
            return [{"thread_id": r[0], "workspace": r[1], "model": r[2]} for r in cur.fetchall()]

    def get_task_type(self, thread_id: int) -> Optional[str]:
        """获取任务类型"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT task_type FROM tasks WHERE thread_id = ?", (thread_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def is_resumable(self, thread_id: int) -> bool:
        """检查任务是否可继续"""
        task = self.get_task_by_thread(thread_id)
        return task is not None and task["status"] == STATUS_COMPLETED
