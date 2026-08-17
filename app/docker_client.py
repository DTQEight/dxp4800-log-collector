import struct
import logging
from app.config import Config

logger = logging.getLogger(__name__)


class DockerClient:
    """轻量 Docker 客户端：只保留 list_running_containers / inspect / tail_runtime。

    日志拉取不再走 Docker SDK（走 json-log 直读文件通道），但：
      - 前端"运行时"Tab 需要临时拉一下容器实时 stdout 给用户预览
      - 启动时需要 list/inspect 拿容器元数据
    """

    def __init__(self):
        try:
            import docker  # type: ignore[import-not-found]
        except Exception as e:
            raise RuntimeError(f"未安装 docker SDK（pip install docker）: {e}") from e

        try:
            base_url = "unix:///var/run/docker.sock"
            self._client = docker.DockerClient(base_url=base_url, version="auto", timeout=30)
            self._client.ping()
            logger.info("Docker API连接成功")
        except Exception as e:
            logger.warning(f"docker SDK初始化失败，容器信息/运行时Tab可能不可用: {e}")
            self._client = None

    # ---------------- 容器列表 / 详情 ----------------
    def list_running_containers(self) -> list[dict]:
        """返回 [{id,name,image,state,created,cpu_percent,memory_usage}]"""
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
                out.append({
                    "id": c.id,
                    "name": c.name.strip("/") or c.id[:12],
                    "image": (attrs.get("Config") or {}).get("Image") or "",
                    "state": state.get("Status") or "running",
                    "created": attrs.get("Created") or "",
                    "cpu_percent": 0.0,
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
                    "state": "running",
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
