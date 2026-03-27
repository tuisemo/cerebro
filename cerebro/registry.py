"""
任务注册表 — 基于 SQLite 的轻量持久化层

用于在机器人重启、崩溃后保留任务元数据，并支持自动清理参考。
"""

import sqlite3
import time
from pathlib import Path
from typing import List, Dict, Any


class TaskRegistry:
    def __init__(self, db_path: str = "./droid_tasks.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        """初始化数据库表结构"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    thread_id   INTEGER PRIMARY KEY,
                    workspace   TEXT NOT NULL,
                    prompt      TEXT,
                    model       TEXT,
                    status      TEXT DEFAULT 'active',
                    last_update REAL NOT NULL
                )
            """)
            conn.commit()

    def register_task(self, thread_id: int, workspace: str, prompt: str = "", model: str = ""):
        """记录新任务或更新现有任务"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO tasks (thread_id, workspace, prompt, model, status, last_update)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (thread_id, workspace, prompt, model, 'active', time.time()))
            conn.commit()

    def update_status(self, thread_id: int, status: str):
        """更新任务状态（如 completed, error）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE tasks SET status = ?, last_update = ? WHERE thread_id = ?
            """, (status, time.time(), thread_id))
            conn.commit()

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
        """获取所有标记为 active 的任务 (用于重启恢复)"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT thread_id, workspace, model FROM tasks WHERE status = 'active'")
            return [{"thread_id": r[0], "workspace": r[1], "model": r[2]} for r in cur.fetchall()]
