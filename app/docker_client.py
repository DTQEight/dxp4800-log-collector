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
            tail: 'all' 或 int；
                  调用方注意不要直接传 'all' 给跑了几个月的容器，
                  否则把 10GB 日志全拉回来 CPU/IO 都会炸。
            since: unix秒(int) 或 None
        """
        try:
            c = self.client.containers.get(container_id_or_name)
            kwargs = {"stream": False, "timestamps": True}
            # tail 参数保护：'all' 时只给最后 MAX_LOG_LINES_PER_TICK 行，避免首跑拉爆
            if tail == "all":
                kwargs["tail"] = Config.MAX_LOG_LINES_PER_TICK
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
                    # TTY 容器直接是文本
                    leftover = _emit_text(leftover + chunk.encode("utf-8", errors="replace"))
                    continue
                buf = leftover + chunk
                # 逐帧解：8 bytes header -> [1 stream][3 pad][4 size big-endian]
                while len(buf) >= 8:
                    try:
                        _, _, frame_len = struct.unpack(">B B I", buf[:8])
                    except struct.error:
                        break
                    total = 8 + frame_len
                    if len(buf) < total:
                        break
                    payload = buf[8:total]
                    buf = buf[total:]
                    # payload 内含 \n，拆成多行吐出
                    leftover = _emit_text(payload, head=leftover, return_leftover=True)
                leftover = buf
        except GeneratorExit:
            return
        except Exception as e:
            logger.debug(f"日志流解码异常: {e}")
            # 把 leftover 以文本形式吐出来，不要丢
            if leftover:
                for line in leftover.decode("utf-8", errors="replace").splitlines():
                    if line: yield line


def _emit_text(buf: bytes, head: bytes = b"", return_leftover=False):
    """把 buf 里能拼的整行 yield 出来，返回残留的半行（未换行）bytes。"""
    # 这个函数把"拼接 + 解码 + 拆行"合并，减少一次 splitlines 拷贝
    if not buf and not head:
        return b"" if return_leftover else None
    full = head + buf
    try:
        text = full.decode("utf-8", errors="replace")
    except Exception:
        return full if return_leftover else None
    if not text:
        return b"" if return_leftover else None
    # text 末尾是否以 \n 结尾，决定是否有 leftover
    if text.endswith("\n"):
        lines = text.splitlines()
        for ln in lines:
            if ln: yield ln
        return b"" if return_leftover else None
    # 不以 \n 结尾：最后一段是半行先存着
    idx = text.rfind("\n")
    if idx == -1:
        # 完全没有换行：全部进 leftover
        leftover_text = text
        lines: list[str] = []
    else:
        lines = text[:idx].splitlines()
        for ln in lines:
            if ln: yield ln
        leftover_text = text[idx + 1 :]
    if return_leftover:
        return leftover_text.encode("utf-8", errors="replace")
    return None


def strip_docker_log_headers(text: str) -> str:
    """
    对一次性 get_container_logs(stream=False) 的文本兜底去除帧头。
    非 TTY 容器里 docker SDK 返回的文本，每行前面可能带不可见的 8 字节头，
    直接写进日志会污染内容 + 正则解析多花 CPU。
    """
    if not text:
        return text
    # 用前 32 字符快速判断：是否含控制字符（'\x01'/'\x02' + 后跟 0x00）
    sample = text[:64]
    if "\x01" not in sample and "\x02" not in sample:
        return text   # 大概率 TTY，不用处理
    try:
        raw = text.encode("utf-8", errors="replace")
    except Exception:
        return text
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        if i + 8 <= n and raw[i] in (1, 2) and raw[i + 1] == 0 and raw[i + 2] == 0 and raw[i + 3] == 0:
            try:
                frame_len = struct.unpack(">I", raw[i + 4 : i + 8])[0]
            except struct.error:
                # 不正常，向后跳 1 字节继续找
                i += 1
                continue
            end = i + 8 + frame_len
            if end > n:
                end = n
            chunk = raw[i + 8 : end]
            if chunk:
                out.append(chunk.decode("utf-8", errors="replace"))
            i = end
        else:
            # TTY 部分或混了 TTY：找到下一个 \n 直接拷贝
            j = raw.find(b"\n", i)
            if j == -1:
                out.append(raw[i:].decode("utf-8", errors="replace"))
                break
            out.append(raw[i : j + 1].decode("utf-8", errors="replace"))
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
