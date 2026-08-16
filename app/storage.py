import os
import re
from datetime import datetime, date
from app.config import Config
import logging

logger = logging.getLogger(__name__)

TIMESTAMP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z?)\s(.*)$", re.DOTALL)


class LogStorage:
    """负责按容器+日期分类写入日志文件"""

    @staticmethod
    def _container_dir(container_name: str) -> str:
        d = os.path.join(Config.LOG_STORAGE_PATH, container_name)
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _today_file(container_name: str) -> str:
        d = LogStorage._container_dir(container_name)
        return os.path.join(d, f"{date.today().isoformat()}.log")

    @staticmethod
    def append_log(container_name: str, raw_line: str):
        """解析并写入单行日志，返回解析后的(timestamp, content)"""
        raw_line = raw_line.rstrip("\n")
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

        fpath = LogStorage._today_file(container_name)
        try:
            with open(fpath, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {content}\n")
        except Exception as e:
            logger.error(f"写入日志文件失败 {fpath}: {e}")

        return ts, content

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
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                if tail and tail > 0:
                    lines = f.readlines()
                    return "".join(lines[-tail:])
                return f.read()
        except Exception as e:
            logger.error(f"读取日志文件失败 {fpath}: {e}")
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
            # 空目录也删掉
            try:
                if not os.listdir(cdir):
                    shutil.rmtree(cdir)
            except Exception:
                pass
        if removed:
            logger.info(f"清理过期日志文件: 删除 {removed} 个文件")
        return removed
