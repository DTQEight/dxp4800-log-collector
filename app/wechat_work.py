"""企业微信通知模块（自建应用 → 文本消息推送）

参考 115transfer 项目中的企业微信模块，针对"日志告警通知"场景做了简化：
- 只保留 access_token 缓存 + 文本消息发送 两个核心能力。
- 支持可选 HTTP 代理（NAS 不能直连公网时通过外部代理转发）。
- 所有对外接口都返回 (ok: bool, msg: str)，方便上层记录日志或回写 API。

企业微信 API 文档：
- 获取 token: https://developer.work.weixin.qq.com/document/path/91039
- 发送消息:   https://developer.work.weixin.qq.com/document/path/90236
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from app.config import Config

logger = logging.getLogger(__name__)

# ===== access_token 缓存（进程级单例，多线程共享）=====
_token_lock = threading.Lock()
_access_token: str | None = None
_token_expires_at: float = 0.0   # 提前 5 分钟过期，避免边界上调用失败


def _build_proxies() -> dict | None:
    """根据配置返回 requests 用的 proxies 字典；不配置代理时返回 None。"""
    url = (Config.WECHAT_WORK_PROXY_URL or "").strip()
    if not url:
        return None
    return {"http": url, "https": url}


def _clear_token() -> None:
    """强制让下次请求重新拿 token（token 失效时调用）。"""
    global _access_token, _token_expires_at
    with _token_lock:
        _access_token = None
        _token_expires_at = 0.0


def get_access_token(force_refresh: bool = False) -> str | None:
    """获取企业微信 access_token，带本地缓存（默认提前 5 分钟过期）。

    失败返回 None，错误已记录到 logger，调用方自行处理。
    """
    global _access_token, _token_expires_at

    # 不强制刷新时，先用本地缓存
    if not force_refresh:
        with _token_lock:
            if _access_token and time.time() < _token_expires_at:
                return _access_token

    if not Config.WECHAT_WORK_CORPID or not Config.WECHAT_WORK_CORPSECRET:
        logger.warning("企业微信 corpid/corpsecret 未配置，无法获取 access_token")
        return None

    url = (
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        f"?corpid={Config.WECHAT_WORK_CORPID}"
        f"&corpsecret={Config.WECHAT_WORK_CORPSECRET}"
    )
    try:
        resp = requests.get(url, timeout=10, proxies=_build_proxies())
        result = resp.json()
    except Exception as e:
        logger.error(f"获取企业微信 access_token 网络异常: {e}")
        return None

    if result.get("errcode") != 0:
        logger.error(
            "获取企业微信 access_token 失败: errcode=%s errmsg=%s",
            result.get("errcode"), result.get("errmsg"),
        )
        return None

    with _token_lock:
        _access_token = result["access_token"]
        # 提前 300s 过期，规避 token 边界失效
        _token_expires_at = time.time() + int(result.get("expires_in", 7200)) - 300
    return _access_token


def send_wechat_message(
    content: str,
    to_user: str | None = None,
) -> tuple[bool, str]:
    """发送一条文本消息到企业微信。

    Args:
        content: 消息正文（text 类型，最长 2048 字节，超出会被自动截断）。
        to_user: 接收人，留空则使用 Config.WECHAT_WORK_TOUSER。

    Returns:
        (ok, msg): ok=True 表示发送成功；msg 是给人看的描述。
    """
    if not Config.WECHAT_WORK_ENABLED:
        return False, "企业微信通知未启用 (WECHAT_WORK_ENABLED=false)"
    if not Config.WECHAT_WORK_AGENTID:
        return False, "未配置 WECHAT_WORK_AGENTID"

    # 企业微信 text 消息上限 2048 字节，按字节截断（中文友好）
    content = _truncate_to_bytes(content or "", 2000)

    token = get_access_token()
    if not token:
        return False, "获取 access_token 失败，请检查 corpid/corpsecret/网络"

    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    payload = {
        "touser": to_user or Config.WECHAT_WORK_TOUSER or "@all",
        "msgtype": "text",
        "agentid": Config.WECHAT_WORK_AGENTID,
        "text": {"content": content},
        # 企业微信要求显式声明重复消息过滤策略
        "duplicate_check_interval": 60,
    }

    try:
        resp = requests.post(
            url, json=payload, timeout=10, proxies=_build_proxies()
        )
        result = resp.json()
    except Exception as e:
        logger.error(f"发送企业微信消息网络异常: {e}")
        return False, f"网络异常: {e}"

    errcode = result.get("errcode")
    if errcode == 0:
        return True, "发送成功"

    # token 失效（42001 / 40014）则强制刷新一次再重试
    if errcode in (40014, 42001):
        logger.warning("access_token 失效，强制刷新后重试一次")
        token = get_access_token(force_refresh=True)
        if token:
            url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
            try:
                resp = requests.post(
                    url, json=payload, timeout=10, proxies=_build_proxies()
                )
                result = resp.json()
                if result.get("errcode") == 0:
                    return True, "发送成功（重试后）"
            except Exception as e:
                logger.error(f"重试发送企业微信消息网络异常: {e}")
                return False, f"重试网络异常: {e}"

    errmsg = result.get("errmsg", "未知错误")
    logger.error(f"发送企业微信消息失败: errcode={errcode} errmsg={errmsg}")
    return False, f"errcode={errcode} errmsg={errmsg}"


def _truncate_to_bytes(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断字符串，避免超长被企业微信拒绝。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # 截到字节边界后再 decode，截断点正好落在多字节字符中间时会用 U+FFFD 替换
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + "…"
