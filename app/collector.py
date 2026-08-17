import threading
import time
import logging
import hashlib
from datetime import datetime
from collections import deque
from app.config import Config
from app.docker_client import DockerClient
from app.json_log_reader import JsonLogReader
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
        # 指纹去重：set 做 O(1) 查重，deque 做 LRU 淘汰（maxlen 满了自动 pop 最老的）
        # 单 deque + `in` 线性查找是 O(N)，N=5000 时每行查重都很贵，CPU 飙升的元凶
        self._seen_fp_set: set[str] = set()
        self._seen_fp_deque: deque[str] = deque(maxlen=5000)

        self._flush_thread: threading.Thread | None = None

        # ====== 企业微信通知：每容器冷却 + 异步发送 ======
        # key: container_name -> 最近一次通知的 monotonic 时间戳
        self._wechat_notify_lock = threading.Lock()
        self._wechat_last_notify: dict[str, float] = {}
        # 同一容器冷却期内累积的错误行数（用于通知正文里提示"还有 N 条同类错误"）
        self._wechat_pending_count: dict[str, int] = {}

        # ====== json-log 直读通道（优先于 Docker SDK）======
        # 直接读 /var/lib/docker/containers/<cid>/<cid>-json.log 文件
        # 绕过 Docker daemon，性能更好、时间戳零歧义
        self._json_reader: JsonLogReader | None = None
        if Config.USE_JSON_LOG_READER:
            reader = JsonLogReader(Config.DOCKER_CONTAINERS_PATH, Config.INITIAL_TAIL_LINES)
            if reader.is_available():
                self._json_reader = reader
                logger.info(
                    "json-log 直读已启用: %s（优先通道，不可读时自动回退 Docker SDK）",
                    Config.DOCKER_CONTAINERS_PATH,
                )
            else:
                logger.info(
                    "json-log 目录不可读: %s，全部走 Docker SDK 通道",
                    Config.DOCKER_CONTAINERS_PATH,
                )

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

            # ===== 通道1：json-log 直读（优先，绕过 Docker daemon）=====
            # 直接读 /var/lib/docker/containers/<cid>/<cid>-json.log 文件
            # 优势：快10倍、时间戳从JSON time字段拿(100%精准)、offset零漏零重
            if self._json_reader:
                result = self._json_reader.read_incremental(cid, cname)
                if result is not None:
                    # json-log 文件存在且可读（即使 lines 为空也表示通道可用）
                    lines, new_offset, is_initial = result
                    if lines:
                        written = self._dedupe_and_feed(cid, cname, "file", lines, initial_pull=is_initial)
                        logger.info(
                            f"[{cname}] file读{len(lines)}行→写入{written}条, offset={new_offset}",
                        )
                    elif is_initial:
                        logger.info(f"[{cname}] file首跑无新日志, offset={new_offset}")
                    continue
                # result is None → 文件不可读（非 json-file driver / 路径不存在），回退 Docker SDK

            # ===== 通道2：Docker SDK logs API（兜底）=====
            # 增量拉日志：用 "since"，首跑只拉最近 INITIAL_TAIL_LINES 行（不翻历史）
            since = self._container_last_since.get(cid)
            initial_pull = since is None   # since=None → 首次启动：不翻历史，只取最近 N 行拿 since 起点
            if initial_pull:
                # 首次启动：只拉最近 INITIAL_TAIL_LINES 行（默认 100），目的是拿到
                # "最近一条日志的时间戳"作为后续 since 起点，不关心几周前的老日志。
                # 省 CPU / 省内存 / 不触发老错误推送。
                tail_arg = Config.INITIAL_TAIL_LINES
            else:
                # 后续增量：传 since 限定时间范围，tail=0 让 docker 不限制行数（由 since 保护）
                tail_arg = 0
            try:
                raw_logs = self.docker.get_container_logs(
                    cid,
                    tail=tail_arg,
                    since=int(since) if since else None,
                )
            except Exception as e:
                logger.error(f"[{cname}] 拉日志失败: {e}")
                continue

            if raw_logs:
                lines = raw_logs.splitlines()
                n_in = len(lines)
                # 跟踪下一轮 since：用拉到的最后一行日志的实际时间戳（不是程序运行时间）
                # 这样可以避免网络/磁盘卡顿导致漏日志
                last_ts_unix = self._extract_last_log_unix_ts(lines)
                written = self._dedupe_and_feed(cid, cname, "pull", lines, initial_pull=initial_pull)

                # ===== 关键修复：written > 0 才推进 since =====
                # 如果拉了 n_in 行但 written=0（全部解析失败/重复），
                # 一旦推进 since 就会导致 since>实际日志时间，后续永远拉不到新日志。
                # 这是"前面几个容器有日志，后面的容器永久没日志"的核心根因。
                if written > 0:
                    if last_ts_unix is not None:
                        # +1 秒避免下一轮 Docker since= 含等号又把这条重复拉回来
                        self._container_last_since[cid] = last_ts_unix + 1
                    else:
                        # 有写入但没解析出 Docker 时间戳（极少见），回退用当前时间
                        self._container_last_since[cid] = time.time()
                    logger.info(
                        f"[{cname}] 拉{n_in}行→写入{written}条, since={int(self._container_last_since[cid])}",
                    )
                elif n_in > 0:
                    # 拉到了行但一条都没写入 → 解析/去重全部被丢弃
                    # - initial_pull 首跑：必须给一个 since 锚点避免永远重拉同一批历史
                    #   优先用能解析到的最后一行真实时间戳+1（未来日志时钟一定>这个值），
                    #   解析不到再用当前时间兜底（最坏情况=跳过首跑这批老日志，避免死循环）
                    # - 增量非首跑：保持原 since 不变，防止新日志真正产生时被跳过
                    if initial_pull:
                        if last_ts_unix is not None:
                            self._container_last_since[cid] = last_ts_unix + 1
                        else:
                            self._container_last_since[cid] = time.time()
                        logger.warning(
                            f"[{cname}] 首跑拉{n_in}行但0条写入(解析失败/重复), "
                            f"since锚定={int(self._container_last_since[cid])} (last_ts解析={'OK' if last_ts_unix is not None else 'FAIL'}), "
                            f"样例首尾行: {lines[0][:120]!r} | ... | {lines[-1][:120]!r}",
                        )
                    else:
                        logger.warning(
                            f"[{cname}] 增量拉{n_in}行但0条写入(全部重复/解析失败), "
                            f"保持原since不推进, 避免漏日志. 样例行: {lines[0][:120]!r}",
                        )
                # else: n_in == 0 但 raw_logs 非空（理论上不会），什么也不做
            else:
                # 这一轮没新日志，since 保持不变（下一轮继续从老位置拉，防止漏）
                if initial_pull:
                    # 首跑即使没日志也要打个桩，避免下次还走 initial_pull
                    self._container_last_since[cid] = time.time()
                    logger.info(f"[{cname}] 首跑无日志, since打桩={int(self._container_last_since[cid])}")

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
                # 批量缓冲：攒够 50 行或 500ms 再一次性 _dedupe_and_feed
                # 否则每行都走完整流程（解析+hash+锁+查重+append），日志多的容器 CPU 直接飙
                buf: list[str] = []
                last_flush = time.monotonic()
                for line in stream:
                    if self._stop_event.is_set():
                        break
                    if not line:
                        continue
                    buf.append(line)
                    now = time.monotonic()
                    if len(buf) >= 50 or (now - last_flush) >= 0.5:
                        self._dedupe_and_feed(
                            container_id, container_name, "stream", buf, initial_pull=False
                        )
                        buf.clear()
                        last_flush = now
                # 流正常结束（容器停止等），把残余的刷掉
                if buf:
                    self._dedupe_and_feed(
                        container_id, container_name, "stream", buf, initial_pull=False
                    )
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

    def _extract_last_log_unix_ts(self, lines: list[str]) -> float | None:
        """从一批日志行里解析"最后一行"的 unix 时间戳，用于下一轮 since。

        Docker timestamps=True 时每行格式：
            2026-08-17T10:52:22.123456789Z content...
            2026-08-17T10:52:22.123456789+08:00 content...
        """
        if not lines:
            return None
        # 从后往前找，跳过空行
        from app.storage import TIMESTAMP_PATTERN, parse_timestamp_to_local
        for line in reversed(lines):
            if not line or not line.strip():
                continue
            m = TIMESTAMP_PATTERN.match(line)
            if not m:
                continue
            ts_raw = m.group(1)
            dt = parse_timestamp_to_local(ts_raw)
            if dt is None:
                continue
            # aware datetime → unix 秒（浮点）
            return dt.timestamp()
        return None

    def _dedupe_and_feed(self, cid, cname, source, lines, initial_pull: bool = False):
        """解析+去重+进缓冲。返回：实际写入缓冲的条数

        Args:
            initial_pull: True 表示这是"首次启动全量拉历史"的结果，默认不触发
                企业微信错误通知（否则首次启动会收到一堆 N 天前的老错误）。
                可通过 WECHAT_WORK_NOTIFY_ON_INIT=true 改成"老错误也通知"。
        """
        if not lines:
            return 0
        written = 0
        # 是否允许这一轮触发错误通知：
        # - 增量/流：永远允许
        # - 首次全量拉历史：只在 WECHAT_WORK_NOTIFY_ON_INIT=true 时允许
        allow_notify = (not initial_pull) or Config.WECHAT_WORK_NOTIFY_ON_INIT
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
                if fp in self._seen_fp_set:
                    continue
                # O(1) 查重通过，加入 set + deque（deque 满了自动 pop 最老的）
                self._seen_fp_set.add(fp)
                self._seen_fp_deque.append(fp)
                # deque 淘汰了老指纹时，同步从 set 里删掉（保持一致）
                # deque.maxlen 触发自动 popleft，无法 hook，所以定期对齐
                if len(self._seen_fp_deque) >= self._seen_fp_deque.maxlen:
                    # 偶尔（达到上限时）重建 set，避免 set 一直涨
                    self._seen_fp_set = set(self._seen_fp_deque)
                q.append(raw)
                self._db_rows.append((cid, cname, ts, source, content))
                written += 1

                # 错误日志检测（在锁内只做轻量的字符串包含判断，不发 IO）
                if allow_notify and first_error is None and self._is_error_line(cname, content):
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
