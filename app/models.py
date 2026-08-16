import sqlite3
import threading
from datetime import datetime, timedelta
from app.config import Config
from app.storage import iso_local, now_local   # 统一本地时区工具
import logging
import os
import atexit

logger = logging.getLogger(__name__)

_db_lock = threading.Lock()

# 用长连接（按线程缓存），避免每次 insert/open-close 两次磁盘操作
_conn_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)
    conn = getattr(_conn_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(Config.DB_PATH, check_same_thread=False, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # ===== 低占用关键调优 =====
        # WAL：读写不互斥，写入比默认 journal 快几倍，同时CPU耗更少(fsync少)
        # NORMAL：不再每个事务 fsync，交给 OS flush；断电/容器炸可能丢最近几秒，日志场景完全可接受
        # mmap_size：用内存映射，读大表搜索时省 CPU 拷贝
        pragmas = (
            ("journal_mode", "WAL"),
            ("synchronous",  "NORMAL"),
            ("cache_size",   -32768),     # 32MB page cache
            ("mmap_size",    256 * 1024 * 1024),
            ("temp_store",   "MEMORY"),
            ("busy_timeout", 30000),
        )
        for k, v in pragmas:
            try:
                conn.execute(f"PRAGMA {k}={v}")
            except Exception as e:
                logger.debug(f"PRAGMA {k}={v} skipped: {e}")
        _conn_local.conn = conn
    return conn


@atexit.register
def _close_conn():
    conn = getattr(_conn_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def init_db():
    """初始化数据库表"""
    with _db_lock:
        conn = _get_conn()
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
        # 覆盖 content 的 FTS5 索引会让 LIKE '%keyword%' 更快，但会占更多空间且需要额外写入
        # 对嵌入式低CPU场景，保持简单B树索引就好
    logger.info("数据库初始化完成 (WAL+NOMRAL)")


def upsert_container(container_info: dict):
    """批量容器也按单个写，量级小可接受；last_seen/first_seen 统一本地时区 iso"""
    with _db_lock:
        conn = _get_conn()
        now = iso_local(now_local())
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


def insert_log_entry(container_id, container_name, timestamp, source, content):
    """保留单行接口，用于外部代码；内部推荐 insert_log_entries 批量"""
    with _db_lock:
        conn = _get_conn()
        conn.execute("""
            INSERT INTO log_entries (container_id, container_name, timestamp, source, content)
            VALUES (?, ?, ?, ?, ?)
        """, (container_id, container_name, timestamp, source, content))


def insert_log_entries(rows):
    """批量插入（核心性能点）：rows 是 list/tuple of (cid,cname,ts,source,content)"""
    if not rows:
        return 0
    with _db_lock:
        conn = _get_conn()
        conn.execute("BEGIN")
        try:
            conn.executemany("""
                INSERT INTO log_entries (container_id, container_name, timestamp, source, content)
                VALUES (?, ?, ?, ?, ?)
            """, rows)
            conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise
    return len(rows)


def list_containers():
    with _db_lock:
        conn = _get_conn()
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

    with _db_lock:
        conn = _get_conn()
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def cleanup_old_logs(days: int):
    """清理N天前的旧日志（按本地时区的截止点，分批删不占锁太久）"""
    cutoff_dt = now_local().replace(microsecond=0) - timedelta(days=days)
    cutoff = iso_local(cutoff_dt)
    total = 0
    with _db_lock:
        conn = _get_conn()
        while True:
            cur = conn.execute(
                "DELETE FROM log_entries WHERE timestamp < ? LIMIT 5000",
                (cutoff,),
            )
            n = cur.rowcount or 0
            total += n
            if n < 5000:
                break
    if total:
        logger.info(f"清理旧日志: 删除 {total} 条记录 (cutoff_local={cutoff})")
    try:
        with _db_lock:
            _get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    return total
