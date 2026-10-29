"""批次D 后续测试：表格 HTML→Markdown 转换 / 引擎降级 / vision provider 分流"""
import pytest

from app.rag.table_ocr import html_table_to_markdown, recognize_table_markdown


class TestHtmlTableToMarkdown:
    def test_basic_table(self):
        html = ("<html><body><table><tr><td>姓名</td><td>部门</td><td>天数</td>"
                "</tr><tr><td>张三</td><td>人事部</td><td>5</td></tr>"
                "</table></body></html>")
        md = html_table_to_markdown(html)
        assert "| 姓名 | 部门 | 天数 |" in md
        assert "| 张三 | 人事部 | 5 |" in md
        assert "---" in md

    def test_colspan_pads_empty(self):
        """colspan=2 的单元格右补空列（行列对齐）"""
        html = ("<table><tr><td>姓名</td><td>部门</td></tr>"
                "<tr><td colspan=\"2\">合并单元格</td></tr></table>")
        md = html_table_to_markdown(html)
        assert "| 合并单元格 |  |" in md

    def test_empty_html_returns_empty(self):
        assert html_table_to_markdown("") == ""
        assert html_table_to_markdown("<html><body></body></html>") == ""

    def test_malformed_returns_empty(self):
        """异常 HTML 不抛异常（转换失败静默空串）"""
        assert html_table_to_markdown("不是 HTML <<<") == ""

    def test_cell_escaping_protects_structure(self):
        """批次D后续质检修复回归钉住：单元格含 | 或换行时转义——
        不打破 Markdown 列结构（OCR 文本常见的干扰字符）"""
        html = ("<table><tr><td>甲|乙</td><td>多" + "\n" +
                "行</td></tr><tr><td>正常</td><td>值</td></tr></table>")
        md = html_table_to_markdown(html)
        assert "甲\\|乙" in md        # | 被转义（结构竖线 3 个 + 转义竖线 1 个）
        assert "多 行" in md           # 换行折叠为空格
        assert "| 正常 | 值 |" in md   # 正常行结构不受影响

    def test_backtick_replaced(self):
        """批次D后续质检低危#1：单元格内反引号替换为单引号——
        单个反引号会开启行内代码区间并吞掉后续 | 分隔符（OCR 代码截图）"""
        html = "<table><tr><td>`pip install`|x</td><td>值</td></tr></table>"
        md = html_table_to_markdown(html)
        assert "'pip install'" in md
        assert "`" not in md


class TestRecognizeTable:
    def test_engine_unavailable_returns_empty(self, monkeypatch):
        """引擎初始化失败 → 空串（降级纯 OCR 文本路径）"""
        import app.rag.table_ocr as mod

        monkeypatch.setattr(mod, "_table_engine", False)
        assert recognize_table_markdown(None, []) == ""

    def test_exception_returns_empty(self, monkeypatch):
        class FakeEngine:
            def __call__(self, *a, **k):
                raise RuntimeError("模型炸了")

        import app.rag.table_ocr as mod

        monkeypatch.setattr(mod, "_table_engine", FakeEngine())
        assert recognize_table_markdown(None, []) == ""


class TestVisionProviderRouting:
    def test_openai_compat_route(self, monkeypatch):
        """provider=openai_compat 且配 base → 走本地端点（图片 base64 直传）"""
        import httpx

        from app.config import settings as cfg
        from app.rag.picture import describe_image

        captured = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "本地描述"}}]}

            @property
            def text(self):
                return ""

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update({"url": url, "json": json, "headers": headers})
            return FakeResp()

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setattr(cfg, "vision_provider", "openai_compat")
        monkeypatch.setattr(cfg, "vision_api_base", "http://local:8000/v1")
        monkeypatch.setattr(cfg, "vision_api_key", "local-key")

        # 用真实存在的图片文件（魔数内容即可——base64 编码不需要解码）
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "t.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
            result = describe_image(str(img))

        assert result == "本地描述"
        assert captured["url"] == "http://local:8000/v1/chat/completions"
        assert captured["headers"]["Authorization"] == "Bearer local-key"
        assert captured["json"]["messages"][0]["content"][1]["type"] == "image_url"
        assert "data:image/png;base64," in \
            captured["json"]["messages"][0]["content"][1]["image_url"]["url"]

    def test_dashscope_default_route(self, monkeypatch):
        """默认 provider 走 dashscope（不触发 openai_compat 路径）"""
        from app.config import settings as cfg
        from app.rag import picture

        monkeypatch.setattr(cfg, "vision_provider", "dashscope")
        called = {"n": 0}

        def fake_dashscope(file_path):
            called["n"] += 1
            return ""

        monkeypatch.setattr(picture, "_describe_dashscope", fake_dashscope)
        picture.describe_image("x.png")
        assert called["n"] == 1
