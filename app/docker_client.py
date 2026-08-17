import docker
from docker.errors import DockerException, NotFound
from app.config import Config
import logging
import struct

logger = logging.getLogger(__name__)


class DockerClient:
    """封装Docker API客户端，用于与绿联NAS上的Docker daemon交互"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_client()
        return cls._instance

    def _init_client(self):
        try:
            self.client = docker.DockerClient(base_url=Config.DOCKER_SOCKET)
            self.client.ping()
            logger.info("Docker API连接成功")
        except DockerException as e:
            logger.error(f"无法连接到Docker API: {e}")
            raise

    # ---------------- 容器列表 ----------------
    def list_running_containers(self):
        """获取所有运行中的容器（排除配置中的黑名单）"""
        # 用 API 的 filters/limit 能省一点返回体
        containers = self.client.containers.list()
        exclude = set(Config.EXCLUDE_CONTAINERS)
        result = []
        for c in containers:
            name = c.name.lstrip("/")
            if name in exclude:
                continue
            # image.tags 对大镜像列表的解析很费 CPU，取不到就给 'none'
            try:
                image = c.image.tags[0] if c.image and c.image.tags else "none"
            except Exception:
                image = "none"
            result.append({
                "id": c.id,
                "name": name,
                "image": image,
                "status": c.status,
            })
        return result

    # ---------------- 一次性拉（供增量收集器用） ----------------
    def get_container_logs(self, container_id_or_name, tail=1000, since=None):
        """获取指定容器的日志

        Args:
            tail: 'all' / 0 / int：
                  - 'all' → 只给最后 MAX_LOG_LINES_PER_TICK 行（首跑保护，避免拉爆）
                  - 0     → 不限制行数（增量场景配合 since 用，由 since 决定范围）
                  - int>0 → 限制最后 N 行（受 MAX_LOG_LINES_PER_TICK 上限保护）
            since: unix秒(int) 或 None；只返回该时间点之后的日志
        """
        try:
            c = self.client.containers.get(container_id_or_name)
            kwargs = {"stream": False, "timestamps": True}
            # tail 参数保护：'all' 时只给最后 MAX_LOG_LINES_PER_TICK 行，避免首跑拉爆
            if tail == "all":
                kwargs["tail"] = Config.MAX_LOG_LINES_PER_TICK
            elif tail in (0, "0"):
                # 0 = 不限制行数，让 since 来决定范围；不加 tail 参数
                # 但配合 since 时 Docker 默认仍可能返回大量历史，再戴上 MAX 保护
                if since is None:
                    kwargs["tail"] = Config.MAX_LOG_LINES_PER_TICK
                # else: since 已限定范围，不限制行数
            else:
                try:
                    t = int(tail)
                    kwargs["tail"] = max(1, min(t, Config.MAX_LOG_LINES_PER_TICK))
                except (TypeError, ValueError):
                    kwargs["tail"] = Config.MAX_LOG_LINES_PER_TICK
            if since:
                try:
                    kwargs["since"] = int(since)
                except (TypeError, ValueError):
                    pass
            logs = c.logs(**kwargs)
            text = logs.decode("utf-8", errors="replace") if isinstance(logs, bytes) else logs
            # 非 TTY 容器 Docker 会在每条日志前加 8 字节头（stream+frame size）
            # 纯文本输出里就会残留一堆 \x01\x00... 乱码前置字符，CPU 上额外反复处理
            return strip_docker_log_headers(text)
        except NotFound:
            logger.warning(f"容器未找到: {container_id_or_name}")
            return None
        except Exception as e:
            logger.error(f"获取容器日志失败 {container_id_or_name}: {e}")
            return None

    # ---------------- 流式拉（供 STREAM_ENABLED=true 时用） ----------------
    def stream_container_logs(self, container_id_or_name, since=None):
        """流式获取容器日志。迭代项是一行行的字符串（已剥 8 字节头）"""
        try:
            c = self.client.containers.get(container_id_or_name)
            kwargs = {"stream": True, "timestamps": True}
            if since:
                try: kwargs["since"] = int(since)
                except Exception: pass
            raw_stream = c.logs(**kwargs)
            return self._decode_stream(raw_stream)
        except Exception as e:
            logger.error(f"建立日志流失败 {container_id_or_name}: {e}")
            return None

    def _decode_stream(self, raw_stream):
        """Docker 流以 chunk 吐出，每个 chunk 内含多条带 8 字节帧头的记录。
        这里输出纯文本行，让上层线程不再反复处理二进制。"""
        leftover = b""
        try:
            for chunk in raw_stream:
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                buf = leftover + chunk
                # 逐帧解：8 bytes header -> [1 stream][3 pad][4 size big-endian]
                while len(buf) >= 8:
                    if buf[0] in (1, 2) and buf[1] == 0 and buf[2] == 0 and buf[3] == 0:
                        frame_len = struct.unpack(">I", buf[4:8])[0]
                        total = 8 + frame_len
                        if len(buf) < total:
                            break
                        payload = buf[8:total]
                        buf = buf[total:]
                        for line in payload.decode("utf-8", errors="replace").splitlines():
                            if line:
                                yield line
                    else:
                        # 非 Docker 帧头（TTY 容器），按 \n 拆行
                        nl = buf.find(b"\n")
                        if nl == -1:
                            break
                        line = buf[:nl].decode("utf-8", errors="replace")
                        if line:
                            yield line
                        buf = buf[nl + 1:]
                leftover = buf
        except GeneratorExit:
            return
        except Exception as e:
            logger.debug(f"日志流解码异常: {e}")
            if leftover:
                for line in leftover.decode("utf-8", errors="replace").splitlines():
                    if line:
                        yield line


def strip_docker_log_headers(text: str) -> str:
    """对 get_container_logs(stream=False) 的文本去除 8 字节帧头（非 TTY 容器）。"""
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


def clean_raw_log_lines(text: str) -> str:
    """对 /tail API 返回的原始 Docker 日志做清洗（让前端看到的和归档文件一致）：

    1. 剥 8 字节帧头（非 TTY 容器）
    2. 逐行处理：剥 Docker UTC 前缀 → 转本地时区 → 剥 ANSI 颜色码 → 剥应用重复时间戳
    3. 没识别到 Docker 前缀的行原样保留（仅剥 ANSI）

    这样前端不管是看归档文件还是 /tail 直拉，显示格式都统一干净。
    """
    if not text:
        return text
    # 延迟导入避免循环引用
    from app.storage import (
        TIMESTAMP_PATTERN, ANSI_PATTERN, APP_TS_PATTERN,
        parse_timestamp_to_local, iso_local, now_local, strip_ansi,
    )

    text = strip_docker_log_headers(text)
    out_lines: list[str] = []
    for line in text.splitlines():
        if not line:
            continue
        content = line
        ts_local = None
        m = TIMESTAMP_PATTERN.match(line)
        if m:
            ts_raw, content = m.group(1), m.group(2)
            ts_local = parse_timestamp_to_local(ts_raw)
        # 剥 ANSI + 应用重复时间戳
        content = strip_ansi(content).lstrip()
        content = APP_TS_PATTERN.sub("", content, count=1).lstrip()
        if ts_local is not None:
            out_lines.append(f"[{iso_local(ts_local)}] {content}")
        else:
            out_lines.append(content)
    return "\n".join(out_lines) + ("\n" if out_lines else "")
