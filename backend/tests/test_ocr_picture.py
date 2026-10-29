"""批次D 测试：OCR 扫描件判定 / picture 合成 / vision 静默降级"""
import pytest

from app.rag.ocr import is_scanned_pdf
from app.rag.picture import build_picture_text, describe_image


class TestScannedPdfDetection:
    def test_empty_text_is_scanned(self):
        assert is_scanned_pdf("", page_count=3)

    def test_low_density_is_scanned(self):
        """每页平均 < 50 字符 → 扫描件（照片型 PDF）"""
        assert is_scanned_pdf("第 1 页只有标题\n" * 2, page_count=4)

    def test_normal_text_not_scanned(self):
        assert not is_scanned_pdf("制度内容" * 100, page_count=3)

    def test_zero_pages_guard(self):
        """page_count=0 防除零（按 1 页算）"""
        assert is_scanned_pdf("", page_count=0)
        assert not is_scanned_pdf("内容" * 100, page_count=0)

    def test_page_count_changes_verdict(self):
        """批次D 质检 P2-2 回归钉住：多页杂文本（页码/印章）按页数判定——
        同文本（54 字符）：page_count=1 时 54≥50 判正常、page_count=10 时
        每页 5.4 字符判扫描件——页数是判定关键参数，不可缺省退化"""
        messy = "第 1 页\n第 2 页\n第 3 页\n" * 3  # 54 字符的杂文本
        assert not is_scanned_pdf(messy, page_count=1)
        assert is_scanned_pdf(messy, page_count=10)


class TestImageMagicNumbers:
    def test_png_and_jpg_magic_validated(self):
        """批次D 质检 P3：图片魔数校验测试（伪造头部被拒）"""
        import tempfile
        from pathlib import Path

        from app.pipeline.loader import validate_file

        with tempfile.TemporaryDirectory() as tmp:
            # 真 PNG 头
            png = Path(tmp) / "a.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
            assert validate_file(str(png))
            # 伪造：txt 内容冒充 png
            fake_png = Path(tmp) / "b.png"
            fake_png.write_bytes(b"plain text not a png")
            assert not validate_file(str(fake_png))
            # 真 JPG 头
            jpg = Path(tmp) / "c.jpg"
            jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"fake")
            assert validate_file(str(jpg))
            # 伪造 jpg
            fake_jpg = Path(tmp) / "d.jpg"
            fake_jpg.write_bytes(b"not a jpeg")
            assert not validate_file(str(fake_jpg))


class TestBuildPictureText:
    def test_both_parts_combined(self):
        text = build_picture_text("流程图.png", "第一步 提交", "这是报销流程的描述")
        assert "【图片文档】流程图.png" in text
        assert "【图片内容描述】" in text
        assert "这是报销流程的描述" in text
        assert "【图中文字】" in text
        assert "第一步 提交" in text

    def test_vision_only(self):
        text = build_picture_text("图.png", "", "只有描述")
        assert "【图片内容描述】" in text
        assert "【图中文字】" not in text

    def test_ocr_only(self):
        text = build_picture_text("图.png", "只有OCR文字", "")
        assert "【图中文字】" in text
        assert "【图片内容描述】" not in text


class TestDescribeImage:
    def test_api_error_returns_empty(self, monkeypatch):
        """vision 调用失败 → 空串（静默降级，OCR 文本仍在）"""
        import dashscope

        class FakeResp:
            status_code = 404
            message = "Model not exist."

        monkeypatch.setattr(
            "dashscope.MultiModalConversation.call",
            lambda **k: FakeResp(),
        )
        assert describe_image(r"D:\不存在.png") == ""

    def test_exception_returns_empty(self, monkeypatch):
        import dashscope

        def boom(**k):
            raise RuntimeError("网络炸了")

        monkeypatch.setattr("dashscope.MultiModalConversation.call", boom)
        assert describe_image(r"D:\不存在.png") == ""

    def test_request_timeout_passed(self, monkeypatch):
        """批次E P3 回归钉住：request_timeout=120 必须传给 SDK（防默认
        300s 超时占死线程池的回归）"""
        import dashscope

        captured = {}

        class FakeResp:
            status_code = 200
            output = type("O", (), {
                "choices": [type("C", (), {
                    "message": type("M", (), {"content": [{"text": "描述"}]})(),
                })()],
            })()

        def fake_call(**kwargs):
            captured.update(kwargs)
            return FakeResp()

        monkeypatch.setattr("dashscope.MultiModalConversation.call", fake_call)
        result = describe_image(r"D:\x.png")
        assert captured.get("request_timeout") == 120
        assert result == "描述"
