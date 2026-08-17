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

        # ====== 企业微信通知：每容器冷却 + 异步发送 ======
        # key: container_name -> 最近一次通知的 monotonic 时间戳
        self._wechat_notify_lock = threading.Lock()
        self._wechat_last_notify: dict[str, float] = {}
        # 同一容器冷却期内累积的错误行数（用于通知正文里提示"还有 N 条同类错误"）
        self._wechat_pending_count: dict[str, int] = {}

    # ---------------- 主循环 ----------------
    def run_foreground(self):
        logger.info(
            "日志收集器启动：COLLECT_INTERVAL=%ss, STREAM=%s, "
            "MAX_LINES_PER_TICK=%s, BATCH_FLUSH=%ss/%s行",
            Config.COLLECT_INTERVAL_SEC, Config.STREAM_ENABLED,
            Config.MAX_LOG_LINES_PER_TICK, Config.BATCH_FLUSH_SEC, Config.BATCH_MAX_ENTRIES,
        )
        self._flush_thread = threading.Thread(target=self._flush_loop, name="log-flush", daemon=True)
        self._flush_thread.start()

        # 公开一个"立即flush"开关：前端/用户点手动触发时可以让磁盘立刻写入，
        # 解决"看起来一分钟才更新一次"的观感问题。
        self._poke_flush_event = threading.Event()

        try:
            while not self._stop_event.is_set():
                t0 = time.monotonic()
                self._collect_once()
                # 拉完一轮立刻尝试 flush（让前端轮询能最快拿到）
                self._poke_flush_event.set()
                self._cleanup_stream_threads()
                # 等待下一轮：支持中途被 poke_flush 打断（没用到）
                elapsed = time.monotonic() - t0
                wait_s = max(0.5, Config.COLLECT_INTERVAL_SEC - elapsed)
                self._stop_event.wait(wait_s)
        finally:
            try: self._flush(force=True)
            except Exception as e: logger.warning(f"最后flush出错: {e}")
            logger.info("日志收集器退出")

    def request_immediate_flush(self):
        """外部（如 Web API）可调用：下一个 flush_loop tick 立刻刷盘刷库"""
        self._poke_flush_event.set()
        self._collect_once_weak = getattr(self, '_collect_once_weak', None)  # noqa

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
        # 本轮命中的第一条错误日志（用于通知正文）；同一轮里多条同类只取首条做样本
        first_error: tuple[str, str] | None = None   # (ts_iso, content)
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

                # 错误日志检测（在锁内只做轻量的字符串包含判断，不发 IO）
                if first_error is None and self._is_error_line(cname, content):
                    first_error = (ts, content)

            # 超过批量阈值就触发 flush（但在 flush_loop 里执行更稳，这里只做 hint）
            if len(self._db_rows) >= Config.BATCH_MAX_ENTRIES:
                pass   # flush_loop 会在下一 tick 处理，不在持锁时做 IO

        # 锁外触发通知（异步，不阻塞收集主循环）
        if first_error is not None:
            self._maybe_notify_error(cname, first_error[0], first_error[1])
        return written

    # ---------------- 企业微信错误通知 ----------------
    def _is_error_line(self, cname: str, content: str) -> bool:
        """判断这行日志是否命中错误关键字。

        - 关闭通知 / 未配置关键字 → 直接 False
        - 配置了 INCLUDE_CONTAINERS 白名单时，只对白名单内的容器报警
        """
        if not Config.WECHAT_WORK_ENABLED:
            return False
        if not Config.WECHAT_WORK_ERROR_KEYWORDS:
            return False
        # 白名单：如果配置了，不在白名单内的容器不报警
        if Config.WECHAT_WORK_INCLUDE_CONTAINERS and cname not in Config.WECHAT_WORK_INCLUDE_CONTAINERS:
            return False
        if not content:
            return False
        # 全部小写比对，让 ERROR/Err/err 都能命中
        lowered = content.lower()
        return any(kw.lower() in lowered for kw in Config.WECHAT_WORK_ERROR_KEYWORDS)

    def _maybe_notify_error(self, cname: str, ts_iso: str, content: str) -> None:
        """带每容器冷却的错误通知触发器。

        - 冷却期内不重复发送，只累计 pending_count，等下次冷却到期后第一条新错误带"还有 N 条"一起发。
        - 实际发送放后台线程，避免阻塞日志收集主循环。
        """
        if not Config.WECHAT_WORK_ENABLED:
            return
        try:
            from app.wechat_work import send_wechat_message
        except Exception as e:   # 模块加载失败也不能影响日志收集
            logger.warning(f"加载 wechat_work 模块失败: {e}")
            return

        now_mono = time.monotonic()
        cooldown = max(1, Config.WECHAT_WORK_COOLDOWN_SEC)

        with self._wechat_notify_lock:
            last = self._wechat_last_notify.get(cname, 0.0)
            in_cooldown = (now_mono - last) < cooldown
            # 累计 pending（无论是否在冷却期，都把这一条记上）
            pending = self._wechat_pending_count.get(cname, 0) + 1
            self._wechat_pending_count[cname] = pending
            if in_cooldown:
                # 还在冷却里，跳过；等下次冷却到期再发
                return
            # 出冷却了：本次发送，并清零 pending（包含本次这条）
            self._wechat_last_notify[cname] = now_mono
            self._wechat_pending_count[cname] = 0
            extra_count = pending - 1   # 本轮累计、除本次首条外还有多少条同类错误

        # 截断正文，避免单行超长把整条通知撑爆
        max_len = max(50, Config.WECHAT_WORK_MAX_CONTENT_LEN)
        body = content if len(content) <= max_len else content[:max_len] + "…"

        msg_lines = [
            "【Docker 日志告警】",
            f"容器: {cname}",
            f"时间: {ts_iso}",
        ]
        if extra_count > 0:
            msg_lines.append(f"近 {Config.WECHAT_WORK_COOLDOWN_SEC}s 内同类错误: {extra_count + 1} 条（仅展示首条）")
        msg_lines.append("内容:")
        msg_lines.append(body)
        message = "\n".join(msg_lines)

        threading.Thread(
            target=self._send_wechat_async,
            args=(message,),
            name=f"wechat-notify-{cname[:12]}",
            daemon=True,
        ).start()

    def _send_wechat_async(self, message: str) -> None:
        """后台线程：实际调用企业微信 API，失败只记日志，不影响收集器。"""
        try:
            from app.wechat_work import send_wechat_message
            ok, msg = send_wechat_message(message)
            if ok:
                logger.info(f"企业微信通知发送成功: {msg}")
            else:
                logger.warning(f"企业微信通知发送失败: {msg}")
        except Exception as e:
            logger.error(f"企业微信通知异常: {e}")

    # ---------------- flush 线程（核心：每 N 秒 / 超过条数 批量落盘） ----------------
    def _flush_loop(self):
        while not self._stop_event.is_set():
            tick_sec = 0.3
            slept = 0.0
            triggered = False
            while not self._stop_event.is_set():
                with self._buf_lock:
                    pending = len(self._db_rows)
                elapsed = time.monotonic() - self._last_flush_ts
                # 三种触发条件任一命中：到时间 / 条数满 / 外部 poke（request_immediate_flush）
                if elapsed >= Config.BATCH_FLUSH_SEC or pending >= Config.BATCH_MAX_ENTRIES:
                    triggered = True; break
                if self._poke_flush_event.is_set():
                    # 外部 poke：等 0.5s 再刷，防止用户每 0.1s 点一下就刷一次
                    self._poke_flush_event.clear()
                    triggered = True
                    time.sleep(0.5)
                    break
                time.sleep(tick_sec)
                slept += tick_sec
                if slept > Config.BATCH_FLUSH_SEC + 5:
                    triggered = True; break
            self._flush(force=not triggered)

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
