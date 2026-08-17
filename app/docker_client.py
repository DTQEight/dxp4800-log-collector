import struct
import logging
import threading
from app.config import Config

logger = logging.getLogger(__name__)


class DockerClient:
    """轻量 Docker 客户端：只保留 list_running_containers / inspect / tail_runtime。

    日志拉取不再走 Docker SDK（走 json-log 直读文件通道），但：
      - 前端"运行时"Tab 需要临时拉一下容器实时 stdout 给用户预览
      - 启动时需要 list/inspect 拿容器元数据

    单例模式：避免每个 HTTP 请求都新建 DockerClient + ping()，
    那会刷屏日志并浪费 socket。
    """

    _singleton_lock = threading.Lock()
    _singleton_instance: "DockerClient | None" = None

    @classmethod
    def get_instance(cls) -> "DockerClient":
        """获取全局单例（线程安全）。所有 Web API 都用它，避免重复 ping。"""
        if cls._singleton_instance is None:
            with cls._singleton_lock:
                if cls._singleton_instance is None:
                    cls._singleton_instance = cls()
        return cls._singleton_instance

    def __init__(self):
        # 已有单例时不重复初始化（防止有人误直接 DockerClient()）
        # 注意：单例请用 DockerClient.get_instance()，本构造函数保留是为了兼容旧代码
        try:
            import docker  # type: ignore[import-not-found]
        except Exception as e:
            raise RuntimeError(f"未安装 docker SDK（pip install docker）: {e}") from e

        try:
            base_url = Config.DOCKER_SOCKET
            self._client = docker.DockerClient(base_url=base_url, version="auto", timeout=30)
            self._client.ping()
            logger.info("Docker API连接成功")
        except Exception as e:
            logger.warning(f"docker SDK初始化失败，容器信息/运行时Tab可能不可用: {e}")
            self._client = None

    def _ensure_alive(self):
        """连接掉线时自动重连（不打印 INFO，只在 DEBUG 里记录）。"""
        if self._client is None:
            return
        try:
            self._client.ping()
        except Exception:
            try:
                import docker  # type: ignore[import-not-found]
                self._client = docker.DockerClient(
                    base_url=Config.DOCKER_SOCKET, version="auto", timeout=30,
                )
                self._client.ping()
                logger.info("Docker API重连成功")
            except Exception as e:
                logger.debug(f"Docker API重连失败: {e}")
                self._client = None

    # ---------------- 容器列表 / 详情 ----------------
    def _calc_cpu_percent(self, stats: dict) -> float:
        """从 Docker stats 返回的 cpu_stats + precpu_stats 计算瞬时 CPU%。
        Docker 一次 stats 调用就包含两个采样点，无需两次调用。"""
        try:
            cpu = stats.get("cpu_stats") or {}
            pre = stats.get("precpu_stats") or {}
            cpu_total = cpu.get("cpu_usage", {}).get("total_usage", 0)
            pre_total = pre.get("cpu_usage", {}).get("total_usage", 0)
            cpu_sys = cpu.get("system_cpu_usage", 0)
            pre_sys = pre.get("system_cpu_usage", 0)
            online_cpus = cpu.get("online_cpus", 1) or 1
            # 按 Docker 公式：CPU% = Δcontainer / Δsystem * online_cpus * 100
            delta_cpu = cpu_total - pre_total
            delta_sys = cpu_sys - pre_sys
            if delta_sys > 0 and delta_cpu >= 0:
                return round(delta_cpu / delta_sys * online_cpus * 100.0, 2)
        except Exception:
            pass
        return 0.0

    def list_running_containers(self) -> list[dict]:
        """返回 [{id,name,image,state,created,cpu_percent,memory_usage}]"""
        self._ensure_alive()
        if self._client is None:
            return []
        out: list[dict] = []
        for c in self._client.containers.list(filters={"status": "running"}):
            try:
                attrs = c.attrs or {}
                host_cfg = attrs.get("HostConfig") or {}
                mem_limit_raw = host_cfg.get("Memory") or 0
                mem_limit = int(mem_limit_raw) if mem_limit_raw else 0
                mem_stats = attrs.get("MemoryStats") or {}
                mem_current_raw = mem_stats.get("Usage") or mem_stats.get("MaxUsage") or 0
                mem_current = int(mem_current_raw) if mem_current_raw else 0
                state = attrs.get("State") or {}
                # CPU%：从 stats() 单次调用中取 precpu/cpu 两个采样点差值
                cpu_pct = 0.0
                try:
                    s = c.stats(stream=False, decode=True)
                    if s:
                        cpu_pct = self._calc_cpu_percent(s)
                except Exception:
                    pass
                out.append({
                    "id": c.id,
                    "name": c.name.strip("/") or c.id[:12],
                    "image": (attrs.get("Config") or {}).get("Image") or "",
                    "status": state.get("Status") or "running",
                    "created": attrs.get("Created") or "",
                    "cpu_percent": cpu_pct,
                    "memory_usage": mem_current,
                    "memory_limit": mem_limit,
                    "exclude": False,
                })
            except Exception as e:
                logger.debug(f"解析容器信息失败 {c.short_id}: {e}")
                out.append({
                    "id": c.id,
                    "name": c.name.strip("/") or c.id[:12],
                    "image": "",
                    "status": "running",
                    "created": "",
                    "cpu_percent": 0.0,
                    "memory_usage": 0,
                    "memory_limit": 0,
                    "exclude": False,
                })
        # 应用排除列表（Config.EXCLUDE_CONTAINERS: list[str]）
        excludes = set()
        raw = Config.EXCLUDE_CONTAINERS or []
        if isinstance(raw, str):
            excludes.update(n.strip() for n in raw.split(",") if n.strip())
        else:
            excludes.update(n.strip() for n in raw if isinstance(n, str) and n.strip())
        for item in out:
            item["exclude"] = item["name"] in excludes
        return out

    def get_container(self, container_id: str):
        self._ensure_alive()
        if self._client is None:
            return None
        try:
            return self._client.containers.get(container_id)
        except Exception:
            return None

    # ---------------- 前端"运行时"Tab 实时预览 ----------------
    def tail_runtime(self, container_id: str, n: int = 100) -> list[str]:
        """临时通过 Docker SDK 拉最近 n 行，供用户 UI 预览。
        不写入日志文件，不用于正式采集（采集走 json-log 直读）。"""
        self._ensure_alive()
        if self._client is None:
            return []
        try:
            c = self._client.containers.get(container_id)
            raw = c.logs(tail=n, timestamps=False, stdout=True, stderr=True, stream=False)
            if raw is None:
                return []
            if isinstance(raw, bytes):
                text = raw.decode("utf-8", errors="replace")
            else:
                text = str(raw)
            # 去除 8 字节帧头（非 TTY 容器）
            text = strip_docker_log_headers(text)
            return [ln for ln in text.splitlines() if ln]
        except Exception as e:
            logger.debug(f"tail_runtime 失败 {container_id[:12]}: {e}")
            return []


def strip_docker_log_headers(text: str) -> str:
    """对 tail_runtime 返回的文本去除 8 字节帧头（非 TTY 容器）。"""
    if not text:
        return text
    sample = text[:64]
    if "\x01" not in sample and "\x02" not in sample:
        return text  # TTY 容器，无需处理
    raw = text.encode("utf-8", errors="replace")
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        if i + 8 <= n and raw[i] in (1, 2) and raw[i + 1] == 0 and raw[i + 2] == 0 and raw[i + 3] == 0:
            frame_len = struct.unpack(">I", raw[i + 4: i + 8])[0]
            end = min(i + 8 + frame_len, n)
            chunk = raw[i + 8: end]
            if chunk:
                out.append(chunk.decode("utf-8", errors="replace"))
            i = end
        else:
            j = raw.find(b"\n", i)
            if j == -1:
                out.append(raw[i:].decode("utf-8", errors="replace"))
                break
            out.append(raw[i: j + 1].decode("utf-8", errors="replace"))
            i = j + 1
    return "".join(out)
