"""企业微信回调消息加解密（对应官方 WXBizMsgCrypt 的关键流程）

- 签名：sha1(sort(token, timestamp, nonce, encrypt) 拼接)
- 密文：AES-256-CBC，key = Base64Decode(EncodingAESKey + "=")，iv = key[:16]
- 明文结构：16 字节随机串 + 4 字节大端长度 + msg + PKCS7(32) 填充

参考：https://developer.work.weixin.qq.com/document/path/90968
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_BLOCK_SIZE = 32


class WeChatCryptError(Exception):
    """回调签名校验失败或密文解析失败。"""


def _sha1_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    items = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


def verify_signature(token: str, timestamp: str, nonce: str, encrypt: str, signature: str) -> bool:
    """校验回调签名（encrypt 为空时按明文模式的三元签名）。"""
    if not token or not signature:
        return False
    if encrypt:
        expected = _sha1_signature(token, timestamp, nonce, encrypt)
    else:
        expected = hashlib.sha1(
            "".join(sorted([token, timestamp, nonce])).encode("utf-8")
        ).hexdigest()
    return expected == signature


def _aes_key(aes_key: str) -> bytes:
    try:
        key = base64.b64decode(aes_key + "=")
    except Exception as e:
        raise WeChatCryptError(f"EncodingAESKey 非法: {e}") from e
    if len(key) != 32:
        raise WeChatCryptError("EncodingAESKey 解码后长度不是 32 字节（需 43 位字符串）")
    return key


def decrypt(cipher_text: str, aes_key: str) -> str:
    """解密回调密文，返回明文 XML / 字符串。"""
    key = _aes_key(aes_key)
    try:
        data = base64.b64decode(cipher_text)
    except Exception as e:
        raise WeChatCryptError(f"密文 Base64 解码失败: {e}") from e
    if not data or len(data) % 16 != 0:
        raise WeChatCryptError("密文长度非法")

    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    try:
        plain = decryptor.update(data) + decryptor.finalize()
    except Exception as e:
        raise WeChatCryptError(f"AES 解密失败: {e}") from e

    pad = plain[-1]
    if pad < 1 or pad > _BLOCK_SIZE or plain[-pad:] != bytes([pad]) * pad:
        raise WeChatCryptError("PKCS7 填充非法")
    plain = plain[:-pad]
    if len(plain) < 20:
        raise WeChatCryptError("明文长度不足")
    (msg_len,) = struct.unpack(">I", plain[16:20])
    if msg_len > len(plain) - 20:
        raise WeChatCryptError("明文长度字段非法")
    try:
        return plain[20:20 + msg_len].decode("utf-8")
    except UnicodeDecodeError as e:
        raise WeChatCryptError(f"明文不是 UTF-8: {e}") from e


def signature(token: str, timestamp: str, nonce: str, encrypt_text: str) -> str:
    """生成被动回复用的 msg_signature。"""
    return _sha1_signature(token, timestamp, nonce, encrypt_text)


def encrypt(msg: str, aes_key: str) -> str:
    """把明文加密成回调返回用的密文（16B 随机串 + 4B 长度 + msg + PKCS7）。"""
    key = _aes_key(aes_key)
    data = msg.encode("utf-8")
    plain = os.urandom(16) + struct.pack(">I", len(data)) + data
    pad = _BLOCK_SIZE - (len(plain) % _BLOCK_SIZE)
    plain += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    cipher_text = encryptor.update(plain) + encryptor.finalize()
    return base64.b64encode(cipher_text).decode("ascii")


def extract_encrypt(xml_text: str) -> str:
    """从回调 XML 中取出 <Encrypt> 内容（避免引入完整 XML 解析依赖）。"""
    import re
    m = re.search(r"<Encrypt>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</Encrypt>", xml_text or "", re.S)
    return (m.group(1).strip() if m else "")
