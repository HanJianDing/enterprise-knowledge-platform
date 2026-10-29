"""企业微信消息加解密（批次E·6e 企微对话通道）

企微自建应用回调协议（参考官方 WXBizMsgCrypt Python 示例实现）：
- 消息体：AES-256-CBC 加密（PKCS7 填充，IV=密钥前 16 字节）+ base64
- 签名：SHA1(token, timestamp, nonce, encrypt) 字典序拼接
- 明文格式：<random 16字节><msg_len 4字节网络序><明文 XML><corp_id>
"""
import base64
import hashlib
import struct
import time
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _pkcs7_pad(data: bytes, block_size: int = 32) -> bytes:
    pad = block_size - len(data) % block_size
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    pad = data[-1]
    if pad < 1 or pad > 32 or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS7 填充非法")
    return data[:-pad]


def _aes_key(encoding_aes_key: str) -> bytes:
    return base64.b64decode(encoding_aes_key + "=")


def sign(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """企微签名：sha1 按字典序拼接 token/timestamp/nonce/encrypt"""
    raw = "".join(sorted([token, timestamp, nonce, encrypt]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def encrypt_message(plain_xml: str, encoding_aes_key: str, corp_id: str) -> str:
    """明文 XML → 加密串（base64）。明文前置随机 16 字节+长度+corp_id 尾缀"""
    import os

    raw = (
        os.urandom(16)
        + struct.pack(">I", len(plain_xml.encode("utf-8")))
        + plain_xml.encode("utf-8")
        + corp_id.encode("utf-8")
    )
    cipher = Cipher(
        algorithms.AES(_aes_key(encoding_aes_key)),
        modes.CBC(_aes_key(encoding_aes_key)[:16]),
    )
    enc = cipher.encryptor()
    return base64.b64encode(enc.update(_pkcs7_pad(raw)) + enc.finalize()).decode()


def decrypt_message(encrypt: str, encoding_aes_key: str,
                    corp_id: str | None = None) -> str:
    """加密串 → 明文 XML；corp_id 提供时校验尾缀（防串消息）"""
    cipher = Cipher(
        algorithms.AES(_aes_key(encoding_aes_key)),
        modes.CBC(_aes_key(encoding_aes_key)[:16]),
    )
    dec = cipher.decryptor()
    raw = _pkcs7_unpad(dec.update(base64.b64decode(encrypt)) + dec.finalize())
    msg_len = struct.unpack(">I", raw[16:20])[0]
    plain = raw[20:20 + msg_len].decode("utf-8")
    tail = raw[20 + msg_len:].decode("utf-8", errors="ignore")
    if corp_id is not None and tail != corp_id:
        raise ValueError(f"corp_id 不匹配（期望 {corp_id}，实际 {tail}）")
    return plain


def extract_text_message(plain_xml: str) -> dict | None:
    """从明文 XML 提取消息要素；非文本消息返回 None"""
    try:
        root = ElementTree.fromstring(plain_xml)
        msg_type = root.findtext("MsgType", "")
        if msg_type != "text":
            return None
        return {
            "msg_type": msg_type,
            "from_user": root.findtext("FromUserName", ""),
            "content": root.findtext("Content", ""),
            "msg_id": root.findtext("MsgId", ""),
        }
    except ElementTree.ParseError:
        return None


def build_text_reply_xml(to_user: str, from_user: str, content: str) -> str:
    """构造文本回复 XML（企微被动回复格式）"""
    content_escaped = (
        content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content_escaped}]]></Content>"
        "</xml>"
    )
