import sqlite3
import threading
from datetime import datetime
from app.config import Config
import logging
import os

logger = logging.getLogger(__name__)

_db_lock = threading.Lock()


def _get_conn():
    os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(Config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表"""
    with _db_lock, _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS containers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                image TEXT,
                first_seen TIMESTAMP,
                last_seen TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS log_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container_id TEXT NOT NULL,
                container_name TEXT NOT NULL,
                timestamp TIMESTAMP,
                source TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_container 
            ON log_entries(container_id, timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_time 
            ON log_entries(timestamp)
        """)
        conn.commit()
    logger.info("数据库初始化完成")


def upsert_container(container_info: dict):
    with _db_lock, _get_conn() as conn:
        now = datetime.now().isoformat()
        conn.execute("""
            INSERT INTO containers (id, name, image, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                image=excluded.image,
                last_seen=excluded.last_seen
        """, (
            container_info["id"],
            container_info["name"],
            container_info.get("image"),
            container_info.get("first_seen", now),
            now,
        ))
        conn.commit()


def insert_log_entry(container_id, container_name, timestamp, source, content):
    with _db_lock, _get_conn() as conn:
        conn.execute("""
            INSERT INTO log_entries (container_id, container_name, timestamp, source, content)
            VALUES (?, ?, ?, ?, ?)
        """, (container_id, container_name, timestamp, source, content))
        conn.commit()


def list_containers():
    with _db_lock, _get_conn() as conn:
        cur = conn.execute("SELECT * FROM containers ORDER BY last_seen DESC")
        return [dict(r) for r in cur.fetchall()]


def search_logs(container_id=None, container_name=None, keyword=None,
                start_time=None, end_time=None, limit=500, offset=0):
    sql = "SELECT * FROM log_entries WHERE 1=1"
    params = []
    if container_id:
        sql += " AND container_id = ?"
        params.append(container_id)
    if container_name:
        sql += " AND container_name LIKE ?"
        params.append(f"%{container_name}%")
    if keyword:
        sql += " AND content LIKE ?"
        params.append(f"%{keyword}%")
    if start_time:
        sql += " AND timestamp >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND timestamp <= ?"
        params.append(end_time)
    sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with _db_lock, _get_conn() as conn:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def cleanup_old_logs(days: int):
    """清理N天前的旧日志"""
    cutoff = datetime.fromtimestamp(datetime.now().timestamp() - days * 86400).isoformat()
    with _db_lock, _get_conn() as conn:
        cur = conn.execute("DELETE FROM log_entries WHERE timestamp < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
    if deleted:
        logger.info(f"清理旧日志: 删除 {deleted} 条记录")
    return deleted
