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

    # 收集器配置（默认"前端观感上秒级更新"的档位，家用可放心）
    # 每隔多久去 Docker daemon 拉一次"每个容器的增量日志"
    COLLECT_INTERVAL_SEC = int(os.getenv("COLLECT_INTERVAL_SEC", "60"))

    # 【默认关闭】每个容器一条独立的实时日志流线程
    STREAM_ENABLED = _bool("STREAM_ENABLED", False)

    # 单次从 docker logs 拉取的最大条数（防一次拉几百MB把NAS拉爆）
    MAX_LOG_LINES_PER_TICK = int(os.getenv("MAX_LOG_LINES_PER_TICK", "5000"))

    # 批量落盘 / 批量入库缓冲
    # - 日志停了最多 BATCH_FLUSH_SEC 秒后磁盘文件/DB里一定能看到
    # - 日志量大会更快触发 BATCH_MAX_ENTRIES
    # - 想要"更实时"（比如几秒钟内文件里就出现）可以把 BATCH_FLUSH_SEC 设 2-3
    BATCH_FLUSH_SEC = float(os.getenv("BATCH_FLUSH_SEC", "3.0"))
    BATCH_MAX_ENTRIES = int(os.getenv("BATCH_MAX_ENTRIES", "200"))

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

    # ===== 企业微信通知（监控错误日志，推送到企业微信）=====
    # 总开关：默认关闭，配置好 corpid/corpsecret/agentid 后再开启
    WECHAT_WORK_ENABLED = _bool("WECHAT_WORK_ENABLED", False)
    # 企业 ID（my-enterprise -> 企业信息 -> 企业ID）
    WECHAT_WORK_CORPID = os.getenv("WECHAT_WORK_CORPID", "")
    # 应用的 Secret（自建应用 -> Secret）
    WECHAT_WORK_CORPSECRET = os.getenv("WECHAT_WORK_CORPSECRET", "")
    # 应用的 AgentId（自建应用 -> AgentId，必须是整数）
    WECHAT_WORK_AGENTID = int(os.getenv("WECHAT_WORK_AGENTID", "0") or "0")
    # 接收人：@all 表示全员，也可填指定用户ID（多个用 | 分隔，如 user1|user2）
    WECHAT_WORK_TOUSER = os.getenv("WECHAT_WORK_TOUSER", "@all")
    # 可选：HTTP 代理（NAS 不能直连公网时使用），如 http://192.168.31.1:7890
    WECHAT_WORK_PROXY_URL = os.getenv("WECHAT_WORK_PROXY_URL", "")
    # 错误关键字（逗号分隔）：日志内容命中任一关键字就触发通知
    # 默认覆盖常见日志级别 + 异常堆栈特征
    WECHAT_WORK_ERROR_KEYWORDS = [
        kw.strip()
        for kw in os.getenv(
            "WECHAT_WORK_ERROR_KEYWORDS",
            "ERROR,ERR,FATAL,PANIC,Exception,Traceback,Failed,failed,Out of memory,OOM",
        ).split(",")
        if kw.strip()
    ]
    # 单容器通知冷却（秒）：同一容器在该窗口内只推一条通知，避免错误日志洪水
    WECHAT_WORK_COOLDOWN_SEC = int(os.getenv("WECHAT_WORK_COOLDOWN_SEC", "60"))
    # 单条通知正文最大长度（企业微信 text 消息上限 2048 字节，留点给前缀）
    WECHAT_WORK_MAX_CONTENT_LEN = int(os.getenv("WECHAT_WORK_MAX_CONTENT_LEN", "1500"))
    # 可选：只监控指定容器（逗号分隔，留空=全部容器）。例如 go2rtc,frpc
    WECHAT_WORK_INCLUDE_CONTAINERS = [
        c.strip()
        for c in os.getenv("WECHAT_WORK_INCLUDE_CONTAINERS", "").split(",")
        if c.strip()
    ]
