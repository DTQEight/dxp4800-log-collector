import os
from dotenv import load_dotenv

load_dotenv()

def _bool(name, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")

class Config:
    # Docker配置
    DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "unix:///var/run/docker.sock")

    # 日志存储配置
    LOG_STORAGE_PATH = os.getenv("LOG_STORAGE_PATH", "/app/logs")
    LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))

    # 收集器配置（低占用默认值）
    COLLECT_INTERVAL_SEC = int(os.getenv("COLLECT_INTERVAL_SEC", "120"))

    # 【默认关闭】每个容器一条独立的实时日志流线程
    # 对 DXP4800 这种轻载场景，光靠定时增量拉（120s）已经够，
    # 流式线程在容器多/日志量大时会拉高 CPU/内存，默认 OFF。
    # 要开就设 STREAM_ENABLED=true
    STREAM_ENABLED = _bool("STREAM_ENABLED", False)

    # 单次从 docker logs 拉取的最大条数（防一次拉几百MB把NAS拉爆）
    MAX_LOG_LINES_PER_TICK = int(os.getenv("MAX_LOG_LINES_PER_TICK", "5000"))

    # 批量落盘 / 批量入库缓冲配置（越大越省CPU，内存占用就几KB可忽略）
    BATCH_FLUSH_SEC = float(os.getenv("BATCH_FLUSH_SEC", "10.0"))
    BATCH_MAX_ENTRIES = int(os.getenv("BATCH_MAX_ENTRIES", "500"))

    EXCLUDE_CONTAINERS = [
        c.strip()
        for c in os.getenv("EXCLUDE_CONTAINERS", "dxp4800-log-collector").split(",")
        if c.strip()
    ]

    # Web服务配置
    WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
    WEB_PORT = int(os.getenv("WEB_PORT", "5000"))
    WEB_USERNAME = os.getenv("WEB_USERNAME", "admin")
    WEB_PASSWORD = os.getenv("WEB_PASSWORD", "admin123")

    # 数据库路径
    DB_PATH = os.getenv("DB_PATH", "/app/data/logs.db")
