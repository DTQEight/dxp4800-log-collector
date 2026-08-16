import threading
import time
import logging
from datetime import datetime
from app.config import Config
from app.docker_client import DockerClient
from app.storage import LogStorage
from app import models

logger = logging.getLogger(__name__)


class LogCollector:
    """
    核心日志收集器
    1. 定期扫描正在运行的容器
    2. 对每个容器拉取增量日志并存储
    3. 支持实时流式监听
    """

    def __init__(self):
        self.docker = DockerClient()
        # 记录每个容器上次拉取日志的时间戳，用于增量拉取
        self._container_last_since: dict[str, datetime] = {}
        self._stop_event = threading.Event()
        self._stream_threads: dict[str, threading.Thread] = {}

    def _collect_once(self):
        """执行一轮增量收集"""
        try:
            containers = self.docker.list_running_containers()
        except Exception as e:
            logger.error(f"获取容器列表失败: {e}")
            return

        for info in containers:
            cid = info["id"]
            cname = info["name"]

            # 更新容器元数据
            models.upsert_container(info)

            # 增量拉取日志
            since = self._container_last_since.get(cid)
            since_ts = int(since.timestamp()) if since else None
            raw_logs = self.docker.get_container_logs(
                cid, tail="all" if since_ts is None else 0, since=since_ts
            )
            if raw_logs:
                lines = raw_logs.splitlines()
                for line in lines:
                    ts, content = LogStorage.append_log(cname, line)
                    if ts and content:
                        models.insert_log_entry(cid, cname, ts, "stdout", content)
                self._container_last_since[cid] = datetime.now()
                logger.info(f"[{cname}] 收集 {len(lines)} 行日志")

            # 启动实时流监听（如果还没启动）
            if cid not in self._stream_threads or not self._stream_threads[cid].is_alive():
                t = threading.Thread(
                    target=self._stream_loop, args=(cid, cname), daemon=True
                )
                t.start()
                self._stream_threads[cid] = t

    def _stream_loop(self, container_id: str, container_name: str):
        """单独的线程，实时流式监听单个容器"""
        logger.info(f"[{container_name}] 启动实时日志流")
        backoff = 1
        while not self._stop_event.is_set():
            try:
                stream = self.docker.stream_container_logs(container_id)
                if stream is None:
                    break
                for chunk in stream:
                    if self._stop_event.is_set():
                        break
                    line = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
                    # Docker会把stdout/stderr封装成带头部的字节块，简单处理
                    for sub in line.splitlines():
                        if not sub:
                            continue
                        ts, content = LogStorage.append_log(container_name, sub)
                        if ts and content:
                            models.insert_log_entry(container_id, container_name, ts, "stream", content)
                backoff = 1
            except Exception as e:
                logger.warning(f"[{container_name}] 日志流中断: {e}, {backoff}s后重试")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        logger.info(f"[{container_name}] 日志流线程退出")

    def run_foreground(self):
        """阻塞运行：定期收集+实时流"""
        logger.info("日志收集器启动")
        while not self._stop_event.is_set():
            self._collect_once()
            # 清理已停止容器的流线程
            dead = [k for k, t in self._stream_threads.items() if not t.is_alive()]
            for k in dead:
                self._stream_threads.pop(k, None)
            self._stop_event.wait(Config.COLLECT_INTERVAL_SEC)

    def stop(self):
        self._stop_event.set()
