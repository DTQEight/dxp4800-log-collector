import os
import re
from datetime import datetime, date
from app.config import Config
import logging

logger = logging.getLogger(__name__)

# Docker 注入的 timestamps=True 格式：2026-08-16T09:12:34.567890123Z 或带 ±08:00
TIMESTAMP_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[\.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s(.*)$",
    re.DOTALL,
)

# ANSI 颜色码（go2rtc / 很多 Go/Node 应用会输出 \x1b[90m...\x1b[0m）
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")

# 应用自己打的时间戳（剥掉 ANSI 后检测），匹配常见格式：
#   09:29:50.797          → 纯时间（带可选毫秒）
#   2026-08-17 09:29:50   → 日期+时间
#   2026/08/17 09:29:50   → 斜杠日期+时间
#   Aug 17 09:29:50       → syslog 风格
APP_TS_PATTERN = re.compile(
    r"^(?:"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}[\sT]\d{2}:\d{2}:\d{2}(?:[\.,]\d+)?"
    r"|\d{2}:\d{2}:\d{2}(?:[\.,]\d+)?"
    r"|[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
    r")\s*"
)

# 按容器分别缓存今天的 dir_path / file_path，避免跨容器串写。
# (之前是全局单值：容器 A 写过一次后缓存了 A 的目录，
#  容器 B 进来时 cached_date==today 直接返回 A 的目录，
#  导致 B 的日志写到 A 的文件里，B 自己的目录永远不创建)
# key: container_name  value: (date_obj, dir_path, file_path)
_TODAY_CACHE: dict[str, tuple] = {}


def strip_ansi(text: str) -> str:
    """剥掉 ANSI 颜色码，让日志更干净"""
    return ANSI_PATTERN.sub("", text) if text else text


def now_local() -> datetime:
    """返回"本地时区的现在"（aware datetime），用于 fallback 时间和日常业务。"""
    return datetime.now(Config.LOCAL_TZ)


def today_local() -> date:
    return now_local().date()


def iso_local(dt: datetime) -> str:
    """统一把 aware dt 转为本地时区再 isoformat（毫秒精度，去掉微秒尾部 000 以省空间）。"""
    if not isinstance(dt, datetime):
        raise TypeError("iso_local expects datetime, got %r" % type(dt))
    if dt.tzinfo is None:
        # 朴素 dt：按 LOCAL_TZ 解释（兼容旧库里存的没偏移时间）
        dt = dt.replace(tzinfo=Config.LOCAL_TZ)
    else:
        dt = dt.astimezone(Config.LOCAL_TZ)
    iso = dt.isoformat(timespec="milliseconds")
    # +08:00 -> +08:00 保持；Z 一般不会出现，但也没事
    return iso


def parse_timestamp_to_local(raw: str) -> datetime | None:
    """把 Docker 返回的 UTC / 带偏移时间戳解析 → 转为 LOCAL_TZ 的 aware datetime。"""
    if not raw:
        return None
    s = raw.strip()
    # Python 3.11 fromisoformat 不识别结尾 Z，先替换
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # 支持 2026-08-16T08:00:00+0800 这种无冒号的
    if len(s) >= 6 and s[-3] in "+-" and ":" not in s[-3:]:
        s = s[:-2] + ":" + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # 日志里不带偏移的本地字符串，按 LOCAL_TZ 解释
        dt = dt.replace(tzinfo=Config.LOCAL_TZ)
    else:
        dt = dt.astimezone(Config.LOCAL_TZ)
    return dt


class LogStorage:
    """按容器+（本地时区）日期分类写入日志文件；统一显示本地时区时间。"""

    @staticmethod
    def _today_paths(container_name: str):
        global _TODAY_CACHE
        today = today_local()
        cached = _TODAY_CACHE.get(container_name)
        if cached and cached[0] == today:
            return cached[1], cached[2]
        d = os.path.join(Config.LOG_STORAGE_PATH, container_name)
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, f"{today.isoformat()}.log")
        _TODAY_CACHE[container_name] = (today, d, fp)
        # 日期翻转后，顺便清理一下昨天的过期缓存（保留 31 天以上安全，这里简单防内存泄漏）
        if len(_TODAY_CACHE) > 500:
            _TODAY_CACHE.clear()
            _TODAY_CACHE[container_name] = (today, d, fp)
        return d, fp

    @staticmethod
    def _parse_line(raw_line: str):
        """(ts_local_iso, content) — 剥掉 Docker UTC 前缀 + ANSI 颜色码 + 应用重复时间戳"""
        raw_line = raw_line.rstrip("\r\n")
        if not raw_line:
            return None, None
        content = raw_line
        ts_local: datetime | None = None
        m = TIMESTAMP_PATTERN.match(raw_line)
        if m:
            ts_raw, content = m.group(1), m.group(2)
            ts_local = parse_timestamp_to_local(ts_raw)
        if ts_local is None:
            # 没 Docker 时间戳（比如 TTY/应用自己打了但格式不匹配）→ 用当前本地时间兜底
            ts_local = now_local()

        # 剥 ANSI 颜色码（go2rtc 等应用输出 \x1b[90m...\x1b[0m 包裹时间戳）
        content = strip_ansi(content).lstrip()
        # 检测应用自己打的时间戳，如果有就剥掉（避免和我们的 [ts] 前缀重复显示）
        content = APP_TS_PATTERN.sub("", content, count=1).lstrip()

        return iso_local(ts_local), content

    @staticmethod
    def append_log(container_name: str, raw_line: str):
        ts, content = LogStorage.append_many(container_name, [raw_line])
        if not ts:
            return None, None
        return ts[0], content[0]

    @staticmethod
    def append_many(container_name: str, raw_lines):
        if not raw_lines:
            return [], []
        _, fpath = LogStorage._today_paths(container_name)
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
                return LogStorage._read_tail(fpath, tail)
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            logger.error(f"读取日志文件失败 {fpath}: {e}")
            return ""

    @staticmethod
    def _read_tail(fpath: str, tail: int) -> str:
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
                    chunk *= 2
                lines = data.decode("utf-8", errors="replace").splitlines()
                if len(lines) > tail:
                    lines = lines[-tail:]
                return "\n".join(lines) + ("\n" if lines else "")
        except Exception as e:
            logger.error(f"tail 读取失败 {fpath}: {e}")
            return ""

    @staticmethod
    def cleanup_expired_files(days: int):
        import shutil
        from datetime import timedelta

        cutoff_date = today_local() - timedelta(days=days)
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
                    d = date.fromisoformat(fn[:-4])
                    if d < cutoff_date:
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
