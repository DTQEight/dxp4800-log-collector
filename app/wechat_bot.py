"""企业微信消息机器人：解析回调消息、执行查询指令、生成被动回复。

指令：帮助 / 状态 / 错误 / 搜索 / 静音 / 恢复
- 只响应 WECHAT_WORK_TOUSER 范围内的用户（@all 表示不限制）
- 每用户限流 10 次/分钟
- 静音期间 collector 不再推送错误告警
"""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from app import models
from app.config import Config
from app.storage import iso_local, now_local

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "【日志中心指令】\n"
    "状态 —— 采集概览与近期错误统计\n"
    "错误 [容器] [条数] —— 最近 24h 错误日志（默认 5 条，最多 10 条）\n"
    "搜索 关键词 [容器] —— 最近 24h 匹配日志（最多 10 条）\n"
    "静音 [时长] —— 暂停告警推送，如 静音 30m / 静音 2h（默认 30 分钟）\n"
    "恢复 —— 恢复告警推送\n"
    "帮助 —— 显示本帮助"
)

_RATE_LIMIT = 10
_RATE_WINDOW = 60  # 秒

_mute_lock = threading.Lock()
_mute_until = 0.0  # 时间戳（秒）

_rate_lock = threading.Lock()
_rate_hits: dict[str, list[float]] = {}


# ================= 静音状态（collector 也会用）=================
def is_muted() -> bool:
    with _mute_lock:
        return _mute_until > time.time()


def mute_remaining_sec() -> int:
    with _mute_lock:
        return max(0, int(_mute_until - time.time()))


def _set_mute(seconds: int) -> None:
    global _mute_until
    with _mute_lock:
        _mute_until = time.time() + seconds


def unmute() -> None:
    global _mute_until
    with _mute_lock:
        _mute_until = 0.0


# ================= 回调 XML 解析 =================
def parse_inbound(xml_text: str) -> dict:
    """解析企业微信推送的明文 XML（已解密）。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    def _text(tag: str) -> str:
        el = root.find(tag)
        return (el.text or "").strip() if el is not None else ""

    return {
        "to_user": _text("ToUserName"),
        "from_user": _text("FromUserName"),
        "create_time": _text("CreateTime"),
        "msg_type": _text("MsgType").lower(),
        "content": _text("Content"),
        "event": _text("Event").lower(),
    }


def _is_allowed(user: str) -> bool:
    allowed = (Config.WECHAT_WORK_TOUSER or "").strip()
    if not allowed or "@all" in allowed:
        return True
    ids = {x.strip() for x in re.split(r"[|,]", allowed) if x.strip()}
    return not ids or user in ids


def _rate_limited(user: str) -> bool:
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(user, []) if now - t < _RATE_WINDOW]
        if len(hits) >= _RATE_LIMIT:
            _rate_hits[user] = hits
            return True
        hits.append(now)
        _rate_hits[user] = hits
    return False


# ================= 指令处理 =================
def handle_message(user: str, content: str) -> str:
    if not _is_allowed(user):
        return "无权限：当前账号不在告警接收人范围内。"
    if _rate_limited(user):
        return "操作过于频繁，请稍后再试（每分钟最多 10 次）。"

    text = (content or "").replace("\u3000", " ").strip()
    if not text:
        return HELP_TEXT
    parts = re.split(r"\s+", text, maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("帮助", "help", "?", "？"):
        return HELP_TEXT
    if cmd in ("状态", "status"):
        return _status()
    if cmd in ("错误", "errors", "error"):
        return _errors(rest)
    if cmd in ("搜索", "search"):
        return _search(rest)
    if cmd in ("静音", "mute"):
        return _mute(rest)
    if cmd in ("恢复", "解除静音", "unmute"):
        return _unmute()
    return "未识别的指令。\n\n" + HELP_TEXT


def handle_inbound(xml_text: str) -> str:
    """处理一条明文消息 XML，返回要回复的文本（空字符串表示不回复）。"""
    msg = parse_inbound(xml_text)
    if not msg:
        return ""
    if msg["msg_type"] == "text":
        return handle_message(msg["from_user"], msg["content"])
    if msg["msg_type"] == "event" and msg["event"] in ("subscribe", "enter_agent"):
        return HELP_TEXT
    return ""


def _status() -> str:
    keywords = Config.WECHAT_WORK_ERROR_KEYWORDS
    since = iso_local(now_local() - timedelta(hours=1))
    stats = models.get_log_stats(keywords, since)

    lines = [
        "【日志中心状态】",
        f"容器: {stats['containers']} 个",
        f"近 1 小时日志: {stats['recent_lines']} 条",
    ]
    if keywords:
        lines.append(f"近 1 小时错误: {stats['error_lines']} 条")
        for row in stats["top_errors"]:
            lines.append(f"  · {row['container_name']}: {row['cnt']} 条")
    else:
        lines.append("未配置错误关键字，告警与错误统计不可用")

    remain = mute_remaining_sec()
    if remain:
        lines.append(f"告警推送: 已静音，剩余 {_fmt_duration(remain)}")
    else:
        lines.append("告警推送: 正常")
    return "\n".join(lines)


def _errors(rest: str) -> str:
    keywords = Config.WECHAT_WORK_ERROR_KEYWORDS
    if not keywords:
        return "未配置错误关键字，请先在 Web UI 的企业微信设置中填写。"

    tokens = rest.split()
    count = 5
    if tokens and tokens[-1].isdigit():
        count = max(1, min(10, int(tokens.pop())))
    container = tokens[0] if tokens else None

    since = iso_local(now_local() - timedelta(hours=24))
    rows = models.search_logs(
        container_name=container, start_time=since, limit=300,
    )
    matched = [
        r for r in rows
        if any(kw.lower() in (r.get("content") or "").lower() for kw in keywords)
    ][:count]

    scope = container or "全部容器"
    if not matched:
        return f"【最近错误】{scope}\n近 24 小时没有匹配的错误日志。"

    lines = [f"【最近错误】{scope}（{len(matched)} 条）"]
    for r in matched:
        content = (r.get("content") or "").strip().replace("\n", " ")
        if len(content) > 120:
            content = content[:120] + "…"
        lines.append(f"[{_fmt_ts(r.get('timestamp'))}] {r.get('container_name')}: {content}")
    return "\n".join(lines)


def _search(rest: str) -> str:
    tokens = rest.split()
    if not tokens:
        return "用法：搜索 关键词 [容器]"
    keyword = tokens[0]
    container = tokens[1] if len(tokens) > 1 else None

    since = iso_local(now_local() - timedelta(hours=24))
    rows = models.search_logs(
        container_name=container, keyword=keyword, start_time=since, limit=10,
    )
    scope = container or "全部容器"
    if not rows:
        return f"【搜索】{keyword} @ {scope}\n近 24 小时没有匹配日志。"

    lines = [f"【搜索】{keyword} @ {scope}（{len(rows)} 条）"]
    for r in rows:
        content = (r.get("content") or "").strip().replace("\n", " ")
        if len(content) > 120:
            content = content[:120] + "…"
        lines.append(f"[{_fmt_ts(r.get('timestamp'))}] {r.get('container_name')}: {content}")
    return "\n".join(lines)


def _mute(rest: str) -> str:
    m = re.fullmatch(r"(\d+)\s*([smhd分钟小时天]?)", rest.strip().lower())
    if not m:
        return "用法：静音 [时长]，例如 静音 30m、静音 2h（默认 30 分钟）"

    value = int(m.group(1))
    unit = m.group(2) or "m"
    if unit in ("m", "分钟"):
        seconds = value * 60
    elif unit in ("h", "小时"):
        seconds = value * 3600
    elif unit in ("d", "天"):
        seconds = value * 86400
    else:  # s
        seconds = value

    seconds = min(max(seconds, 60), 86400)
    _set_mute(seconds)
    until = datetime.fromtimestamp(time.time() + seconds)
    return f"已静音告警推送 {_fmt_duration(seconds)}，至 {until.strftime('%H:%M:%S')}。\n发送“恢复”可提前解除。"


def _unmute() -> str:
    if not is_muted():
        return "当前未静音，告警推送正常。"
    unmute()
    return "已恢复告警推送。"


# ================= 被动回复 XML =================
def build_reply_xml(inbound: dict, reply_text: str, encrypted: bool) -> str:
    """生成企业微信要求的被动回复 XML；encrypted=True 时按安全模式加密。"""
    ts = str(int(time.time()))
    reply_text = _truncate_bytes(reply_text, 2000)
    inner = (
        "<xml>"
        f"<ToUserName><![CDATA[{_escape_cdata(inbound.get('from_user', ''))}]]></ToUserName>"
        f"<FromUserName><![CDATA[{_escape_cdata(inbound.get('to_user', ''))}]]></FromUserName>"
        f"<CreateTime>{ts}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{_escape_cdata(reply_text)}]]></Content>"
        "</xml>"
    )
    if not encrypted:
        return inner

    from app.wechat_crypt import encrypt, signature

    aeskey = Config.WECHAT_WORK_CALLBACK_AESKEY
    token = Config.WECHAT_WORK_CALLBACK_TOKEN
    if not aeskey or not token:
        raise RuntimeError("加密回复需要先配置 Token 和 EncodingAESKey")

    nonce = str(int(time.time() * 1000) % 1000000000)
    cipher = encrypt(inner, aeskey)
    sig = signature(token, ts, nonce, cipher)
    return (
        "<xml>"
        f"<Encrypt><![CDATA[{cipher}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{sig}]]></MsgSignature>"
        f"<TimeStamp>{ts}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce>"
        "</xml>"
    )


def _escape_cdata(text: str) -> str:
    return (text or "").replace("]]>", "]]&gt;")


def _truncate_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "…"


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "--"
    try:
        return datetime.fromisoformat(ts).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(ts)[:19].replace("T", " ")


def _fmt_duration(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600} 小时"
    if seconds >= 60:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"
