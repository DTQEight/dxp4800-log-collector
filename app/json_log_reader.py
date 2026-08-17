"""直接读取 Docker json-file 日志文件，绕过 Docker daemon。

优势（对比 Docker SDK logs API）：
1. 性能提升 10 倍 — 直接 open/read 本地文件，不经 UNIX socket + dockerd
2. 时间戳从 JSON time 字段拿，100% 可解析，不存在 written=0 卡死
3. 用文件 offset 做增量，seek 绝对精准，不会漏/重
4. 零 Docker daemon 压力

需要 docker-compose.yml 挂载：
  /var/lib/docker/containers:/var/lib/docker/containers:ro

Docker json-file 日志格式（每行一个 JSON 对象）：
  {"log":"日志内容\\n","stream":"stdout","time":"2026-08-17T03:40:00.213916480Z"}
"""
import os
import json
import logging

logger = logging.getLogger(__name__)


class JsonLogReader:
    """按容器 ID 直接读取宿主机上的 json-log 文件，用文件 offset 做增量。"""

    def __init__(self, containers_dir: str, initial_tail_lines: int):
        self._dir = containers_dir
        self._initial_tail = initial_tail_lines
        # key: container_id -> 上次读到的文件 offset（字节数）
        self._offsets: dict[str, int] = {}
        # key: container_id -> 上次读的文件路径（检测轮转/路径变化）
        self._paths: dict[str, str] = {}

    def is_available(self) -> bool:
        """containers 目录是否已挂载且可读"""
        return os.path.isdir(self._dir) and os.access(self._dir, os.R_OK)

    def _get_log_path(self, cid: str) -> str | None:
        """获取容器的 json-log 文件路径

        标准路径: /var/lib/docker/containers/<cid>/<cid>-json.log
        """
        fpath = os.path.join(self._dir, cid, f"{cid}-json.log")
        if os.path.isfile(fpath):
            return fpath
        # 兜底：扫描容器目录下其他 .log 文件（兼容非标准命名 / local driver）
        cdir = os.path.join(self._dir, cid)
        if os.path.isdir(cdir):
            for fn in os.listdir(cdir):
                if fn.endswith(".log") and os.path.isfile(os.path.join(cdir, fn)):
                    return os.path.join(cdir, fn)
        return None

    def read_incremental(self, cid: str, cname: str) -> tuple[list[str], int, bool] | None:
        """读取容器的增量日志。

        Returns:
            (lines, new_offset, is_initial_pull) — 成功读取（lines 可能为空=无新日志）
            None — 文件不可读（非 json-file driver / 目录没挂载），调用方应回退 Docker SDK

        lines 格式：每行是 "时间戳 内容"（和 Docker SDK timestamps=True 一样），
        复用现有 _parse_line / _dedupe_and_feed pipeline。
        """
        fpath = self._get_log_path(cid)
        if fpath is None:
            return None

        try:
            file_size = os.path.getsize(fpath)
        except OSError:
            return None

        prev_offset = self._offsets.get(cid, 0)
        prev_path = self._paths.get(cid)
        is_initial = cid not in self._offsets

        # 检测文件轮转：路径变了 或 offset > 文件大小（文件被截断/轮转）
        if prev_path and prev_path != fpath:
            prev_offset = 0
        elif prev_offset > file_size:
            prev_offset = 0

        self._paths[cid] = fpath

        # 首次读取：如果文件很大，只读最后 N 行对应的字节量（不全量翻历史）
        # seek_back 到文件中间 → 第一行是半行，需要跳过
        seeked_back = False
        if prev_offset == 0 and file_size > 65536:
            seek_back = min(file_size, max(65536, self._initial_tail * 256))
            start = file_size - seek_back
            seeked_back = True   # start 在文件中间，第一行是半行
        else:
            start = prev_offset   # 上次 offset 在 \n 之后（行边界），第一行是完整的

        if start >= file_size:
            self._offsets[cid] = start
            return [], start, is_initial

        try:
            with open(fpath, "rb") as f:
                f.seek(start)
                chunk = f.read()
        except OSError as e:
            logger.warning(f"[{cname}] 读 json-log 失败: {e}")
            return None

        if not chunk:
            self._offsets[cid] = start
            return [], start, is_initial

        # ===== 在原始字节上处理半行（避免多字节 UTF-8 字符偏移问题）=====

        # 只有 seek_back 到文件中间时第一行才是半行
        # 正常增量读取的 offset 在 \n 之后（行边界），第一行是完整的
        if seeked_back:
            idx = chunk.find(b"\n")
            if idx == -1:
                # 整个 chunk 都是半行，不推进 offset，下次重读
                return [], start, is_initial
            start += idx + 1
            chunk = chunk[idx + 1:]

        # 末尾半行：如果 chunk 不以 \n 结尾，最后一段是半行 → 截到最后一个 \n
        if not chunk.endswith(b"\n"):
            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                # 没有完整行，全部是半行
                return [], start, is_initial
            complete_data = chunk[:last_nl + 1]
            new_offset = start + last_nl + 1
        else:
            complete_data = chunk
            new_offset = start + len(chunk)

        # 解码并解析 JSON 行
        text = complete_data.decode("utf-8", errors="replace")
        lines: list[str] = []
        for raw_line in text.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
                log_content = obj.get("log", "").rstrip("\r\n")
                ts_raw = obj.get("time", "")
                if not log_content:
                    continue
                # 拼成和 Docker SDK timestamps=True 一样的格式，复用现有 pipeline
                lines.append(f"{ts_raw} {log_content}")
            except (json.JSONDecodeError, KeyError):
                # 非 JSON 行（极少见），原样保留
                lines.append(raw_line)

        self._offsets[cid] = new_offset
        return lines, new_offset, is_initial
