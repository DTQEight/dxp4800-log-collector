import os
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

def _bool(name, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def _resolve_tz(name: str):
    """解析 LOCAL_TZ 为 zoneinfo.ZoneInfo（优先）或固定偏移，兜底 UTC+8（Asia/Shanghai）。"""
    try:
        from zoneinfo import ZoneInfo, available_timezones
    except Exception:
        ZoneInfo = None; available_timezones = set()

    name = (name or "").strip()
    if not name:
        name = "Asia/Shanghai"

    if ZoneInfo is not None and (name in available_timezones() or True):
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    # 兼容固定偏移写法 "+08:00" / "UTC+8" / "GMT-8"
    try:
        sign = 1
        if name.startswith("-"): sign = -1; name = name[1:]
        elif name.startswith("+"): name = name[1:]
        for prefix in ("UTC","GMT"):
            if name.upper().startswith(prefix): name = name[len(prefix):]
        h = m = 0
        if ":" in name:
            h_s, m_s = name.split(":", 1); h = int(h_s); m = int(m_s)
        else:
            h = int(float(name))
        return timezone(timedelta(hours=sign*h, minutes=sign*m))
    except Exception:
        return timezone(timedelta(hours=8), "CST")


class Config:
    # Docker配置
    DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "unix:///var/run/docker.sock")

    # 时区：显示/存库/归档文件名 都用这个时区（对中国大陆默认 Asia/Shanghai）
    LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", os.getenv("TZ", "Asia/Shanghai"))
    LOCAL_TZ = _resolve_tz(LOCAL_TZ_NAME)

    # 日志存储配置
    LOG_STORAGE_PATH = os.getenv("LOG_STORAGE_PATH", "/app/logs")
    LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))

    # 收集器配置（低占用默认值）
    COLLECT_INTERVAL_SEC = int(os.getenv("COLLECT_INTERVAL_SEC", "120"))
    STREAM_ENABLED = _bool("STREAM_ENABLED", False)
    MAX_LOG_LINES_PER_TICK = int(os.getenv("MAX_LOG_LINES_PER_TICK", "5000"))
    BATCH_FLUSH_SEC = float(os.getenv("BATCH_FLUSH_SEC", "10.0"))
    BATCH_MAX_ENTRIES = int(os.getenv("BATCH_MAX_ENTRIES", "500"))

    EXCLUDE_CONTAINERS = [
        c.strip()
        for c in os.getenv("EXCLUDE_CONTAINERS", "dxp4800-log-collector").split(",")
        if c.strip()
    ]

    WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
    WEB_PORT = int(os.getenv("WEB_PORT", "5000"))
    WEB_USERNAME = os.getenv("WEB_USERNAME", "admin")
    WEB_PASSWORD = os.getenv("WEB_PASSWORD", "admin123")

    DB_PATH = os.getenv("DB_PATH", "/app/data/logs.db")
