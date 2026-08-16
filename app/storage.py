import os
import re
from datetime import datetime, date
from app.config import Config
import logging

logger = logging.getLogger(__name__)

TIMESTAMP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[\.\d]*Z?)\s(.*)$", re.DOTALL)
# Docker TTY 输出可能没有真正的 RFC3339；保留兼容
_TODAY_CACHE = (None, "", "")  # (date_obj, dir_path, file_path)


class LogStorage:
    """按容器+日期分类写入日志文件"""

    @staticmethod
    def _today_paths(container_name: str):
        """缓存同一日期下的 dir/file 路径，省掉大量 stat()/makedirs()"""
        global _TODAY_CACHE
        today = date.today()
        cached_date, cached_dir, cached_file = _TODAY_CACHE
        if cached_date == today and cached_dir.endswith(container_name):
            return cached_dir, cached_file
        d = os.path.join(Config.LOG_STORAGE_PATH, container_name)
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, f"{today.isoformat()}.log")
        _TODAY_CACHE = (today, d, fp)
        return d, fp

    @staticmethod
    def _parse_line(raw_line: str):
        """解析单行日志 → (timestamp_iso, content)，失败用当前时间兜底"""
        raw_line = raw_line.rstrip("\r\n")
        if not raw_line:
            return None, None
        ts = None
        content = raw_line
        m = TIMESTAMP_PATTERN.match(raw_line)
        if m:
            ts_str, content = m.group(1), m.group(2)
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).isoformat()
            except Exception:
                ts = datetime.now().isoformat()
        else:
            ts = datetime.now().isoformat()
        return ts, content

    @staticmethod
    def append_log(container_name: str, raw_line: str):
        """兼容旧接口：写单行（内部走批量）"""
        ts, content = LogStorage.append_many(container_name, [raw_line])
        if not ts:
            return None, None
        return ts[0], content[0]

    @staticmethod
    def append_many(container_name: str, raw_lines):
        """批量写入（一次 open/write/close）；同时解析并返回 (timestamps, contents) 两个列表。

        没解析成功的行 timestamps/contents 对会跳过（写文件仍保留原文，不丢日志）。
        """
        if not raw_lines:
            return [], []
        _, fpath = LogStorage._today_paths(container_name)

        # 一次遍历：既拼写出串，又做 ts 解析（单遍最省 CPU）
        chunks: list[str] = []
        timestamps: list[str] = []
        contents: list[str] = []
        for raw in raw_lines:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            ts, content = LogStorage._parse_line(line)
            chunks.append(f"[{ts}] {content}\n")
            timestamps.append(ts)
            contents.append(content)

        if chunks:
            try:
                with open(fpath, "a", encoding="utf-8", buffering=128 * 1024) as f:
                    f.write("".join(chunks))
            except Exception as e:
                logger.error(f"写入日志文件失败 {fpath}: {e}")
        return timestamps, contents

    @staticmethod
    def list_container_files(container_name: str):
        d = os.path.join(Config.LOG_STORAGE_PATH, container_name)
        if not os.path.isdir(d):
            return []
        return sorted(
            [f for f in os.listdir(d) if f.endswith(".log")],
            reverse=True,
        )

    @staticmethod
    def read_log_file(container_name: str, filename: str, tail=0) -> str:
        fpath = os.path.join(Config.LOG_STORAGE_PATH, container_name, filename)
        if not os.path.isfile(fpath):
            return ""
        try:
            if tail and tail > 0:
                # 避免把 100MB 文件全读进来再切片；用 seek 从尾部找换行数
                return LogStorage._read_tail(fpath, tail)
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            logger.error(f"读取日志文件失败 {fpath}: {e}")
            return ""

    @staticmethod
    def _read_tail(fpath: str, tail: int) -> str:
        """高效 tail N 行：从文件末尾倒着读块，直到凑够 N 行"""
        avg_line_bytes_est = 200
        chunk = max(avg_line_bytes_est * tail, 8192)
        try:
            size = os.path.getsize(fpath)
        except OSError:
            return ""
        if size == 0:
            return ""
        try:
            with open(fpath, "rb") as f:
                data = b""
                pos = size
                newlines = 0
                while pos > 0 and newlines <= tail:
                    read_sz = min(chunk, pos)
                    pos -= read_sz
                    f.seek(pos)
                    buf = f.read(read_sz)
                    data = buf + data
                    newlines = data.count(b"\n")
                    chunk *= 2     # 不够就加倍读下一次，减少循环次数
                lines = data.decode("utf-8", errors="replace").splitlines()
                if len(lines) > tail:
                    lines = lines[-tail:]
                return "\n".join(lines) + ("\n" if lines else "")
        except Exception as e:
            logger.error(f"tail 读取失败 {fpath}: {e}")
            return ""

    @staticmethod
    def cleanup_expired_files(days: int):
        """清理过期日期的日志文件"""
        import shutil
        from datetime import timedelta

        cutoff = date.today() - timedelta(days=days)
        base = Config.LOG_STORAGE_PATH
        if not os.path.isdir(base):
            return
        removed = 0
        for cname in os.listdir(base):
            cdir = os.path.join(base, cname)
            if not os.path.isdir(cdir):
                continue
            for fn in os.listdir(cdir):
                if not fn.endswith(".log"):
                    continue
                try:
                    d_str = fn[:-4]
                    d = date.fromisoformat(d_str)
                    if d < cutoff:
                        os.remove(os.path.join(cdir, fn))
                        removed += 1
                except Exception:
                    pass
            try:
                if not os.listdir(cdir):
                    shutil.rmtree(cdir)
            except Exception:
                pass
        if removed:
            logger.info(f"清理过期日志文件: 删除 {removed} 个文件")
        return removed
