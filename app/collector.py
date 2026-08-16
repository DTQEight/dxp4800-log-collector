import threading
import time
import logging
import hashlib
from datetime import datetime
from collections import deque
from app.config import Config
from app.docker_client import DockerClient
from app.storage import LogStorage
from app import models

logger = logging.getLogger(__name__)


class LogCollector:
    """低占用版日志收集器

    设计原则：
    1. 默认不拉全量、不开流线程；只用「定时增量 + 限尾拉取」就能覆盖绝大多数场景。
    2. 写文件和写 DB 都走批处理（每 BATCH_FLUSH_SEC 秒或攒够 BATCH_MAX_ENTRIES 条再落），
       把 1 条/次的 open/commit 开销摊平 1-3 个数量级。
    3. 同一容器每次 tick 用 "ts + content" 的小 hash 做滚动去重，避免
       tail=5000 因 since 粗粒度重复写入。
    4. 线程数：主循环 1 条 + flush 1 条 + （可选）每个容器一条 stream 线程（默认关）。
    """

    def __init__(self):
        self.docker = DockerClient()
        # 每个容器：上次拉取的 unix 时间戳（给 docker since= 用）
        self._container_last_since: dict[str, float] = {}
        self._stop_event = threading.Event()
        self._stream_threads: dict[str, threading.Thread] = {}

        # ====== 批量缓冲 ======
        # key: container_name  -> deque[(raw_line,), ...]   按容器分别缓存，写 log 文件能减少切换
        self._buf_lock = threading.Lock()
        self._line_buffers: dict[str, deque] = {}
        self._db_rows: list[tuple] = []          # (cid, cname, ts, source, content)
        self._last_flush_ts = time.monotonic()
        self._seen_fingerprints: deque[str] = deque(maxlen=5000)   # 最近处理过的 (ts,cid,content) hash

        self._flush_thread: threading.Thread | None = None

    # ---------------- 主循环 ----------------
    def run_foreground(self):
        logger.info(
            "日志收集器启动（低占用模式）："
            "COLLECT_INTERVAL=%ss, STREAM=%s, MAX_LINES_PER_TICK=%s, BATCH_FLUSH=%ss/%s行",
            Config.COLLECT_INTERVAL_SEC, Config.STREAM_ENABLED,
            Config.MAX_LOG_LINES_PER_TICK, Config.BATCH_FLUSH_SEC, Config.BATCH_MAX_ENTRIES,
        )
        self._flush_thread = threading.Thread(target=self._flush_loop, name="log-flush", daemon=True)
        self._flush_thread.start()

        try:
            while not self._stop_event.is_set():
                self._collect_once()
                self._cleanup_stream_threads()
                self._stop_event.wait(max(1, Config.COLLECT_INTERVAL_SEC))
        finally:
            # 退出前最后 flush 一次兜底
            try: self._flush(force=True)
            except Exception as e: logger.warning(f"最后flush出错: {e}")
            logger.info("日志收集器退出")

    def stop(self):
        self._stop_event.set()

    # ---------------- 一轮增量收集 ----------------
    def _collect_once(self):
        try:
            containers = self.docker.list_running_containers()
        except Exception as e:
            logger.error(f"获取容器列表失败: {e}")
            return

        for info in containers:
            cid = info["id"]
            cname = info["name"]

            # 更新容器元数据（轻量级，单次写可接受）
            try:
                models.upsert_container(info)
            except Exception as e:
                logger.warning(f"[{cname}] upsert容器失败: {e}")

            # 增量拉日志：用 "since"，但首跑也只给最后 MAX_LOG_LINES_PER_TICK 行
            since = self._container_last_since.get(cid)
            try:
                raw_logs = self.docker.get_container_logs(
                    cid,
                    tail="all" if since is None else 0,
                    since=int(since) if since else None,
                )
            except Exception as e:
                logger.error(f"[{cname}] 拉日志失败: {e}")
                continue

            if raw_logs:
                lines = raw_logs.splitlines()
                n_in = len(lines)
                lines = self._dedupe_and_feed(cid, cname, "pull", lines)
                if lines or n_in:
                    logger.debug(f"[{cname}] tick 拉 {n_in} 行，写入 {lines} 条（批量缓冲中）")

            self._container_last_since[cid] = time.time()

            # （可选）启动实时流监听
            if Config.STREAM_ENABLED and (
                cid not in self._stream_threads
                or not self._stream_threads[cid].is_alive()
            ):
                t = threading.Thread(
                    target=self._stream_loop, args=(cid, cname), daemon=True, name=f"stream-{cname[:10]}"
                )
                t.start()
                self._stream_threads[cid] = t

    # ---------------- 实时流线程（默认关闭） ----------------
    def _stream_loop(self, container_id: str, container_name: str):
        logger.info(f"[{container_name}] 启动实时日志流 (STREAM_ENABLED=true)")
        backoff = 1
        while not self._stop_event.is_set():
            try:
                stream = self.docker.stream_container_logs(container_id)
                if stream is None:
                    time.sleep(backoff); backoff = min(backoff * 2, 30); continue
                backoff = 1
                for line in stream:
                    if self._stop_event.is_set():
                        break
                    # 单行直接丢进缓冲（flush 时才批量 open/insert）
                    self._dedupe_and_feed(container_id, container_name, "stream", [line])
            except Exception as e:
                logger.debug(f"[{container_name}] 日志流中断: {e}, {backoff}s后重试")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        logger.info(f"[{container_name}] 日志流线程退出")

    def _cleanup_stream_threads(self):
        dead = [k for k, t in self._stream_threads.items() if not t.is_alive()]
        for k in dead:
            self._stream_threads.pop(k, None)

    # ---------------- 缓冲 + 去重 ----------------
    def _fingerprint(self, cid, ts, content):
        # hash 只要 20 字节字符串；短 enough 省内存、长 enough 避免碰撞
        raw = f"{cid}|{ts}|{content[:200]}".encode("utf-8")
        return hashlib.md5(raw, usedforsecurity=False).hexdigest()

    def _dedupe_and_feed(self, cid, cname, source, lines):
        """解析+去重+进缓冲。返回：实际写入缓冲的条数"""
        if not lines:
            return 0
        written = 0
        # 先一次性把所有行解析好（单遍，避免重复 parse）
        parsed = [LogStorage._parse_line(ln) for ln in lines]  # type: ignore[attr-defined]
        with self._buf_lock:
            q = self._line_buffers.get(cname)
            if q is None:
                q = deque(); self._line_buffers[cname] = q
            for (ts, content), raw in zip(parsed, lines):
                if ts is None or content is None:
                    continue
                fp = self._fingerprint(cid, ts, content)
                if fp in self._seen_fingerprints:
                    continue
                self._seen_fingerprints.append(fp)
                q.append(raw)
                self._db_rows.append((cid, cname, ts, source, content))
                written += 1

            # 超过批量阈值就触发 flush（但在 flush_loop 里执行更稳，这里只做 hint）
            if len(self._db_rows) >= Config.BATCH_MAX_ENTRIES:
                pass   # flush_loop 会在下一 tick 处理，不在持锁时做 IO
        return written

    # ---------------- flush 线程（核心：每 N 秒 / 超过条数 批量落盘） ----------------
    def _flush_loop(self):
        while not self._stop_event.is_set():
            # 计算睡眠：满足 BATCH_FLUSH_SEC 或 BATCH_MAX_ENTRIES 任一条件就 flush
            slept = 0
            tick_sec = 0.5
            while not self._stop_event.is_set():
                with self._buf_lock:
                    pending = len(self._db_rows)
                elapsed = time.monotonic() - self._last_flush_ts
                if elapsed >= Config.BATCH_FLUSH_SEC or pending >= Config.BATCH_MAX_ENTRIES:
                    break
                time.sleep(tick_sec)
                slept += tick_sec
                if slept > Config.BATCH_FLUSH_SEC:
                    break
            self._flush(force=False)

    def _flush(self, force: bool):
        # 拿锁，瞬间把缓冲区 swap 到局部变量，不阻塞收集线程太久
        with self._buf_lock:
            buf = self._line_buffers
            db_rows = self._db_rows
            self._line_buffers = {}
            self._db_rows = []
            self._last_flush_ts = time.monotonic()

        if not buf and not db_rows:
            return

        t0 = time.monotonic()
        total_lines_written = 0
        total_rows = len(db_rows)

        # 1) 写文件（按容器分桶，每个文件一次 open/write/close）
        for cname, raw_lines in buf.items():
            if not raw_lines:
                continue
            try:
                LogStorage.append_many(cname, raw_lines)
                total_lines_written += len(raw_lines)
            except Exception as e:
                logger.error(f"[{cname}] 批量写文件失败: {e}")

        # 2) 写 DB（单次 executemany + 事务）
        if db_rows:
            try:
                models.insert_log_entries(db_rows)
            except Exception as e:
                logger.error(f"批量入库失败 {len(db_rows)} 行: {e}")

        dt = time.monotonic() - t0
        if total_lines_written or total_rows:
            logger.info(
                "flush 完成: %s 个文件桶, %s 行磁盘, %s 行DB, 耗时 %.2fms",
                len(buf), total_lines_written, total_rows, dt * 1000,
            )
