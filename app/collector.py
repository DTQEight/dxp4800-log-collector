import threading
import time
import logging
import hashlib
from collections import deque
from app.config import Config
from app.docker_client import DockerClient
from app.json_log_reader import JsonLogReader
from app.storage import LogStorage
from app import models

logger = logging.getLogger(__name__)


class LogCollector:
    """单通道日志收集器：直接读宿主机 json-log 文件。

    - 定时增量：每 COLLECT_INTERVAL_SEC 秒遍历一次运行中容器
    - 按文件字节 offset 定位：seek + 增量read，零漏零重
    - 批量缓冲：攒 BATCH_FLUSH_SEC / BATCH_MAX_ENTRIES 再落盘
    - 指纹去重：set + deque LRU，O(1) 查重
    - 企业微信通知：每容器冷却期
    """

    def __init__(self):
        self.docker = DockerClient.get_instance()
        self._stop_event = threading.Event()

        # ====== 指纹去重 ======
        # 用 deque + set 配合做 LRU：满后手动弹出最旧元素并从 set 删除
        # 避免旧方案 set(deque) 全量重建的 O(n) CPU 尖峰
        self._seen_fp_set: set[str] = set()
        self._seen_fp_deque: deque[str] = deque()
        self._seen_fp_maxlen = 5000

        # ====== 批量缓冲 ======
        self._buf_lock = threading.Lock()
        self._line_buffers: dict[str, deque] = {}
        self._db_rows: list[tuple] = []

        self._flush_thread: threading.Thread | None = None
        self._poke_flush_event = threading.Event()

        # ====== 企业微信通知 ======
        self._wechat_notify_lock = threading.Lock()
        self._wechat_last_notify: dict[str, float] = {}
        self._wechat_pending_count: dict[str, int] = {}

        # ====== json-log 直读（唯一通道）======
        self._json_reader: JsonLogReader | None = None
        if Config.USE_JSON_LOG_READER:
            reader = JsonLogReader(
                Config.DOCKER_CONTAINERS_PATH,
                Config.INITIAL_TAIL_LINES,
                Config.MAX_LOG_LINES_PER_TICK,
            )
            if reader.is_available():
                self._json_reader = reader
                logger.info("json-log 直读已启用: %s", Config.DOCKER_CONTAINERS_PATH)
            else:
                logger.error(
                    "json-log 目录不可读: %s。请在 docker-compose.yml 的 volumes: 中添加\n"
                    "  - %s:/var/lib/docker/containers:ro\n"
                    "（把左半部分路径换成 NAS 上 Docker Root Dir/containers）",
                    Config.DOCKER_CONTAINERS_PATH, Config.DOCKER_CONTAINERS_PATH,
                )

    # ---------------- 主循环 ----------------
    def run_foreground(self):
        logger.info(
            "日志收集器启动：COLLECT_INTERVAL=%ss, MAX_LINES_PER_TICK=%s, "
            "BATCH_FLUSH=%ss/%s行",
            Config.COLLECT_INTERVAL_SEC, Config.MAX_LOG_LINES_PER_TICK,
            Config.BATCH_FLUSH_SEC, Config.BATCH_MAX_ENTRIES,
        )
        self._flush_thread = threading.Thread(target=self._flush_loop, name="log-flush", daemon=True)
        self._flush_thread.start()

        try:
            while not self._stop_event.is_set():
                t0 = time.monotonic()
                self._collect_once()
                self._poke_flush_event.set()
                elapsed = time.monotonic() - t0
                wait_s = max(0.5, Config.COLLECT_INTERVAL_SEC - elapsed)
                self._stop_event.wait(wait_s)
        finally:
            try: self._flush()
            except Exception as e: logger.warning(f"最后flush出错: {e}")
            logger.info("日志收集器退出")

    def request_immediate_flush(self):
        self._poke_flush_event.set()

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

            try:
                models.upsert_container(info)
            except Exception as e:
                logger.warning(f"[{cname}] upsert容器失败: {e}")

            if not self._json_reader:
                return  # json-log 通道不可用，主循环也不拉了（避免空转）

            result = self._json_reader.read_incremental(cid, cname)
            if result is None:
                logger.warning(
                    f"[{cname}] json-log 文件不存在或不可读（可能该容器使用了非 json-file driver）。"
                    "建议 docker inspect <容器> --format '{{.HostConfig.LogConfig.Type}}' 确认配置为 json-file。"
                )
                continue

            lines, new_offset, is_initial = result
            if lines:
                written = self._dedupe_and_feed(cid, cname, "file", lines, initial_pull=is_initial)
                logger.info(f"[{cname}] file读{len(lines)}行→写入{written}条, offset={new_offset}")
            elif is_initial:
                logger.info(f"[{cname}] file首跑无新日志, offset={new_offset}")

    # ---------------- 指纹去重 + 入缓冲 ----------------
    def _fingerprint(self, cid, ts, content):
        raw = f"{cid}|{ts}|{content[:200]}".encode("utf-8")
        return hashlib.md5(raw, usedforsecurity=False).hexdigest()

    def _dedupe_and_feed(self, cid, cname, source, lines, initial_pull: bool = False):
        if not lines:
            return 0
        written = 0
        allow_notify = (not initial_pull) or Config.WECHAT_WORK_NOTIFY_ON_INIT
        first_error: tuple[str, str] | None = None

        parsed = [LogStorage.parse_line(ln) for ln in lines]
        with self._buf_lock:
            q = self._line_buffers.get(cname)
            if q is None:
                q = deque(); self._line_buffers[cname] = q
            for (ts, content), raw in zip(parsed, lines):
                if ts is None or content is None:
                    continue
                fp = self._fingerprint(cid, ts, content)
                if fp in self._seen_fp_set:
                    continue
                self._seen_fp_set.add(fp)
                self._seen_fp_deque.append(fp)
                # 增量淘汰：超过上限时弹出最旧指纹，同步从 set 删除（O(1) 而非 O(n) 重建）
                if len(self._seen_fp_deque) > self._seen_fp_maxlen:
                    oldest = self._seen_fp_deque.popleft()
                    self._seen_fp_set.discard(oldest)
                q.append(raw)
                self._db_rows.append((cid, cname, ts, source, content))
                written += 1
                if allow_notify and first_error is None and self._is_error_line(cname, content):
                    first_error = (ts, content)

        if first_error is not None:
            self._maybe_notify_error(cname, first_error[0], first_error[1])

        # 攒够 BATCH_MAX_ENTRIES 条立即触发刷盘，不必等 BATCH_FLUSH_SEC 超时
        if len(self._db_rows) >= Config.BATCH_MAX_ENTRIES:
            self._poke_flush_event.set()
        return written

    # ---------------- 企业微信错误通知 ----------------
    def _is_error_line(self, cname: str, content: str) -> bool:
        if not Config.WECHAT_WORK_ENABLED or not Config.WECHAT_WORK_ERROR_KEYWORDS:
            return False
        if Config.WECHAT_WORK_INCLUDE_CONTAINERS and cname not in Config.WECHAT_WORK_INCLUDE_CONTAINERS:
            return False
        if not content:
            return False
        lowered = content.lower()
        return any(kw.lower() in lowered for kw in Config.WECHAT_WORK_ERROR_KEYWORDS)

    def _maybe_notify_error(self, cname: str, ts_iso: str, content: str) -> None:
        if not Config.WECHAT_WORK_ENABLED:
            return
        try:
            from app.wechat_work import send_wechat_message
        except Exception as e:
            logger.warning(f"加载 wechat_work 模块失败: {e}")
            return

        now_mono = time.monotonic()
        cooldown = max(1, Config.WECHAT_WORK_COOLDOWN_SEC)

        with self._wechat_notify_lock:
            last = self._wechat_last_notify.get(cname, 0.0)
            in_cooldown = (now_mono - last) < cooldown
            pending = self._wechat_pending_count.get(cname, 0) + 1
            self._wechat_pending_count[cname] = pending
            if in_cooldown:
                return
            self._wechat_last_notify[cname] = now_mono
            self._wechat_pending_count[cname] = 0
            extra_count = pending - 1

        max_len = max(50, Config.WECHAT_WORK_MAX_CONTENT_LEN)
        body = content if len(content) <= max_len else content[:max_len] + "…"
        msg_lines = ["【Docker 日志告警】", f"容器: {cname}", f"时间: {ts_iso}"]
        if extra_count > 0:
            msg_lines.append(f"近 {Config.WECHAT_WORK_COOLDOWN_SEC}s 内同类错误: {extra_count + 1} 条（仅展示首条）")
        msg_lines.append("内容:")
        msg_lines.append(body)

        threading.Thread(
            target=self._send_wechat_async, args=("\n".join(msg_lines),),
            name=f"wechat-notify-{cname[:12]}", daemon=True,
        ).start()

    def _send_wechat_async(self, message: str) -> None:
        try:
            from app.wechat_work import send_wechat_message
            ok, msg = send_wechat_message(message)
            if ok:
                logger.info(f"企业微信通知发送成功: {msg}")
            else:
                logger.warning(f"企业微信通知发送失败: {msg}")
        except Exception as e:
            logger.error(f"企业微信通知异常: {e}")

    # ---------------- flush 线程 ----------------
    def _flush_loop(self):
        while not self._stop_event.is_set():
            self._poke_flush_event.wait(timeout=Config.BATCH_FLUSH_SEC)
            self._poke_flush_event.clear()
            time.sleep(0.3)
            self._flush()

    def _flush(self):
        with self._buf_lock:
            buf = self._line_buffers
            db_rows = self._db_rows
            self._line_buffers = {}
            self._db_rows = []

        if not buf and not db_rows:
            return

        t0 = time.monotonic()
        total_lines_written = 0
        total_rows = len(db_rows)

        for cname, raw_lines in buf.items():
            if not raw_lines:
                continue
            try:
                LogStorage.append_many(cname, raw_lines)
                total_lines_written += len(raw_lines)
            except Exception as e:
                logger.error(f"[{cname}] 批量写文件失败: {e}")

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
