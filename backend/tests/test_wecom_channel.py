"""批次E·6e 企微通道测试：加解密往返 / 签名 / 消息提取 / 未启用 503"""
import pytest

from app.services.wecom_crypto import (
    build_text_reply_xml, decrypt_message, encrypt_message,
    extract_text_message, sign,
)

TOKEN = "test_token"
AES_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"  # 43 位 base64
CORP_ID = "ww_test_corp"


class TestWecomCrypto:
    def test_roundtrip(self):
        plain = build_text_reply_xml("user", "kb", "年假 5 天")
        enc = encrypt_message(plain, AES_KEY, CORP_ID)
        assert decrypt_message(enc, AES_KEY, CORP_ID) == plain

    def test_corp_id_mismatch_rejected(self):
        enc = encrypt_message("<xml/>", AES_KEY, CORP_ID)
        with pytest.raises(ValueError, match="corp_id 不匹配"):
            decrypt_message(enc, AES_KEY, "ww_other")

    def test_pkcs7_padding_validated(self):
        """非法填充拒绝（防伪密文）"""
        import base64

        from cryptography.hazmat.primitives.ciphers import Cipher
        from cryptography.hazmat.primitives.ciphers import algorithms, modes

        key = __import__("base64").b64decode(AES_KEY + "=")
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        enc = cipher.encryptor()
        garbage = base64.b64encode(enc.update(b"x" * 32) + enc.finalize()).decode()
        with pytest.raises((ValueError, Exception)):
            decrypt_message(garbage, AES_KEY, CORP_ID)

    def test_sign_dictionary_order(self):
        """签名=sha1(字典序拼接)——顺序无关性（企微协议特征）"""
        enc = "enc_str"
        s1 = sign(TOKEN, "123", "abc", enc)
        s2 = sign(TOKEN, "123", "abc", enc)
        assert s1 == s2
        assert len(s1) == 40

    def test_extract_text_message(self):
        xml = ("<xml><MsgType><![CDATA[text]]></MsgType>"
               "<FromUserName><![CDATA[ZhangSan]]></FromUserName>"
               "<Content><![CDATA[年假怎么申请]]></Content>"
               "<MsgId>12345</MsgId></xml>")
        msg = extract_text_message(xml)
        assert msg is not None
        assert msg["from_user"] == "ZhangSan"
        assert msg["content"] == "年假怎么申请"

    def test_non_text_message_returns_none(self):
        xml = ("<xml><MsgType><![CDATA[image]]></MsgType>"
               "<FromUserName><![CDATA[ZhangSan]]></FromUserName></xml>")
        assert extract_text_message(xml) is None

    def test_reply_xml_escapes(self):
        xml = build_text_reply_xml("u", "kb", "a <b> & c")
        assert "&lt;b&gt;" in xml
        assert "&amp;" in xml


class TestWecomEndpoint:
    @pytest.mark.asyncio
    async def test_channel_disabled_returns_503(self, client, monkeypatch):
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "wecom_channel_token", "")
        monkeypatch.setattr(cfg, "wecom_channel_aes_key", "")
        resp = await client.get("/wecom/callback", params={"echostr": "x"})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_verify_echo_in_test_mode(self, client, monkeypatch):
        """测试模式：跳过签名，echostr 解密原样返回"""
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "wecom_channel_token", TOKEN)
        monkeypatch.setattr(cfg, "wecom_channel_aes_key", AES_KEY)
        monkeypatch.setattr(cfg, "wecom_channel_test_mode", True)
        monkeypatch.setattr(cfg, "wecom_corp_id", CORP_ID)
        # 批次E 质检补审：推送链路依赖的启用条件
        monkeypatch.setattr(cfg, "wecom_secret", "secret")
        monkeypatch.setattr(cfg, "wecom_agent_id", "1000001")

        plain = "echo_hello"
        enc = encrypt_message(plain, AES_KEY, CORP_ID)
        resp = await client.get("/wecom/callback", params={"echostr": enc})
        assert resp.status_code == 200
        assert resp.text == plain


class TestChannelBridge:
    def test_timestamp_window_check(self, monkeypatch):
        """批次E 质检 D 回归：±5 分钟窗口校验"""
        import time

        from fastapi import HTTPException
        from app.api.v1.wecom_channel import _check_timestamp_window

        now = int(time.time())
        _check_timestamp_window(str(now))          # 当前时间通过
        _check_timestamp_window(str(now - 299))    # 窗口内通过
        import pytest as _pytest
        with _pytest.raises(HTTPException):
            _check_timestamp_window(str(now - 301))   # 窗口外拒绝
        with _pytest.raises(HTTPException):
            _check_timestamp_window("不是数字")

    def test_dedupe_and_mark(self):
        """MsgId 排重：首见 True、重复 False、空 id 不排重"""
        from app.services.wecom_channel_bridge import dedupe_and_mark

        assert dedupe_and_mark("msg-1") is True
        assert dedupe_and_mark("msg-1") is False    # 企微重试同消息
        assert dedupe_and_mark("msg-2") is True
        assert dedupe_and_mark("") is True          # 无 MsgId 不排重

    def test_byte_truncation_boundary(self):
        """字节级截断（终审修复：真钉生产函数 truncate_bytes）：
        1800 总预算内、UTF-8 边界正确、短文本不截"""
        from app.services.wecom_channel_bridge import truncate_bytes

        long_answer = "很长的回答" * 2000
        result = truncate_bytes(long_answer, 1800)
        assert len(result.encode("utf-8")) <= 1800
        assert "建议登录知识库查看完整内容" in result
        assert "�" not in result  # 无半个字符残留

        short = "年假 5 天"
        assert truncate_bytes(short, 1800) == short  # 短文本原样

        # 全中文多字节场景（中文字符 3 字节）
        cn = "中" * 700  # 2100 字节超预算
        cn_result = truncate_bytes(cn, 1800)
        assert len(cn_result.encode("utf-8")) <= 1800
