import os
import hashlib
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

def _bool(name, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def _resolve_tz(name: str):
    """解析时区名为 ZoneInfo 或固定偏移，兜底 UTC+8。"""
    try:
        from zoneinfo import ZoneInfo
    except Exception:
        ZoneInfo = None

    name = (name or "").strip() or "Asia/Shanghai"

    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    # 兼容固定偏移写法 "+08:00" / "UTC+8" / "GMT-8"
    try:
        sign = 1
        if name.startswith("-"): sign = -1; name = name[1:]
        elif name.startswith("+"): name = name[1:]
        for prefix in ("UTC", "GMT"):
            if name.upper().startswith(prefix): name = name[len(prefix):]
        h = m = 0
        if ":" in name:
            h_s, m_s = name.split(":", 1); h = int(h_s); m = int(m_s)
        else:
            h = int(float(name))
        return timezone(timedelta(hours=sign * h, minutes=sign * m))
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

    # 收集器配置（json-log 直读单通道）
    # 每隔多久遍历一次运行中容器，检查有无新日志
    # 默认 15s：错误通知时效和 CPU 之间的平衡；想更省可调 30-60
    COLLECT_INTERVAL_SEC = int(os.getenv("COLLECT_INTERVAL_SEC", "15"))

    # 首次启动时每个容器只拉最近 N 行（从 json-log 文件尾部 seek）
    # 默认 100：省 CPU / 省内存 / 不收几周前老错误；补全历史可调大到 5000+
    INITIAL_TAIL_LINES = int(os.getenv("INITIAL_TAIL_LINES", "100"))

    # 单轮读文件的最大行数（防一次拉几百MB把NAS拉爆）
    MAX_LOG_LINES_PER_TICK = int(os.getenv("MAX_LOG_LINES_PER_TICK", "5000"))

    # 批量落盘 / 批量入库缓冲
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

    # ===== json-log 直读（唯一通道）=====
    # 直接读 /var/lib/docker/containers/<cid>/<cid>-json.log 文件，绕过 Docker daemon
    # 必须在 docker-compose.yml 挂载 containers 目录，否则无法读取日志
    USE_JSON_LOG_READER = _bool("USE_JSON_LOG_READER", True)
    DOCKER_CONTAINERS_PATH = os.getenv("DOCKER_CONTAINERS_PATH", "/var/lib/docker/containers")

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
    # 首次启动时，拉到的历史日志里的错误是否也发通知
    #   true  = 老错误也报（排障时可能有用，但首次启动容易收到一堆老日志）
    #   false = 只报"本次启动之后新产生"的错误（默认，符合直觉）
    WECHAT_WORK_NOTIFY_ON_INIT = _bool("WECHAT_WORK_NOTIFY_ON_INIT", False)


# ===== 运行时配置持久化（UI 修改的参数存 JSON，重启后保留）=====
# 只持久化 UI 可调的参数（不含企业微信模块）
_RUNTIME_CONFIG_PATH = os.path.join(
    os.path.dirname(os.getenv("DB_PATH", "/app/data/logs.db")),
    "runtime_config.json",
)

# 密码最小长度
WEB_PASSWORD_MIN_LEN = 6


def _hash_password(pwd: str) -> str:
    """对密码做 SHA-256 哈希，返回带前缀的字符串以便区分明文/哈希。"""
    h = hashlib.sha256(("dxp4800:" + pwd).encode("utf-8")).hexdigest()
    return f"sha256:{h}"


def _verify_password(input_pwd: str, stored: str) -> bool:
    """校验密码：stored 以 sha256: 开头则哈希比对，否则明文比对（兼容环境变量传入的明文）。"""
    if not stored:
        return False
    if stored.startswith("sha256:"):
        return _hash_password(input_pwd) == stored
    return input_pwd == stored

# UI 可调参数的键名 + 类型转换函数（不含企业微信模块）
UI_ADJUSTABLE = {
    "COLLECT_INTERVAL_SEC":    int,
    "INITIAL_TAIL_LINES":      int,
    "LOG_RETENTION_DAYS":      int,
    "BATCH_FLUSH_SEC":         float,
    "BATCH_MAX_ENTRIES":       int,
    "MAX_LOG_LINES_PER_TICK":  int,
    "EXCLUDE_CONTAINERS":      str,   # 逗号分隔字符串，运行时拆成 list
    "WEB_USERNAME":            str,
    "WEB_PASSWORD":            str,
}


def load_runtime_config():
    """启动时从 JSON 文件加载 UI 修改过的配置，覆盖 Config 类属性。"""
    import json
    try:
        with open(_RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    for key, conv in UI_ADJUSTABLE.items():
        if key not in saved:
            continue
        val = saved[key]
        try:
            if key == "EXCLUDE_CONTAINERS":
                setattr(Config, key, [c.strip() for c in str(val).split(",") if c.strip()])
            elif conv is int:
                setattr(Config, key, int(val))
            elif conv is float:
                setattr(Config, key, float(val))
            else:
                setattr(Config, key, str(val))
        except (ValueError, TypeError):
            pass


def save_runtime_config(updates: dict) -> dict:
    """保存 UI 修改的配置到 JSON 文件，同时更新 Config 类属性。

    Returns: {"updated": [...], "rejected": [...]}
    """
    import json
    updated = []
    rejected = []
    for key, val in updates.items():
        if key not in UI_ADJUSTABLE:
            rejected.append(key)
            continue
        try:
            if key == "EXCLUDE_CONTAINERS":
                setattr(Config, key, [c.strip() for c in str(val).split(",") if c.strip()])
            elif key == "WEB_PASSWORD":
                # 密码强度校验
                pwd_str = str(val)
                if len(pwd_str) < WEB_PASSWORD_MIN_LEN:
                    rejected.append(key)
                    continue
                # 存哈希而非明文，防止 runtime_config.json 泄露密码
                setattr(Config, key, _hash_password(pwd_str))
            elif UI_ADJUSTABLE[key] is int:
                setattr(Config, key, int(val))
            elif UI_ADJUSTABLE[key] is float:
                setattr(Config, key, float(val))
            else:
                setattr(Config, key, str(val))
            updated.append(key)
        except (ValueError, TypeError):
            rejected.append(key)
    existing = {}
    try:
        with open(_RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    for key in updated:
        existing[key] = getattr(Config, key) if key != "EXCLUDE_CONTAINERS" else ",".join(Config.EXCLUDE_CONTAINERS)
    try:
        os.makedirs(os.path.dirname(_RUNTIME_CONFIG_PATH), exist_ok=True)
        with open(_RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except OSError as e:
        import logging
        logging.getLogger(__name__).error(f"保存运行时配置失败: {e}")
    return {"updated": updated, "rejected": rejected}


def get_ui_config() -> dict:
    """返回 UI 可调参数的当前值（给前端用）。密码不返回真实值，只返回是否已设置。"""
    return {
        "COLLECT_INTERVAL_SEC":    Config.COLLECT_INTERVAL_SEC,
        "INITIAL_TAIL_LINES":      Config.INITIAL_TAIL_LINES,
        "LOG_RETENTION_DAYS":      Config.LOG_RETENTION_DAYS,
        "BATCH_FLUSH_SEC":         Config.BATCH_FLUSH_SEC,
        "BATCH_MAX_ENTRIES":       Config.BATCH_MAX_ENTRIES,
        "MAX_LOG_LINES_PER_TICK":  Config.MAX_LOG_LINES_PER_TICK,
        "EXCLUDE_CONTAINERS":      ",".join(Config.EXCLUDE_CONTAINERS),
        "WEB_USERNAME":            Config.WEB_USERNAME,
        "WEB_PASSWORD":            "",
        "WEB_PASSWORD_SET":        bool(Config.WEB_PASSWORD),
    }


# 启动时自动加载持久化配置
load_runtime_config()
