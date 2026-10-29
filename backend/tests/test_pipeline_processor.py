"""文档处理管道测试 — 文本清洗等逻辑"""
import os
import tempfile
import pytest

from app.pipeline.processor import _clean_text, _friendly_error, process_document


# ════════════════════════════════════════════════════
# _clean_text 测试
# ════════════════════════════════════════════════════

class TestCleanText:
    """文本清洗测试"""

    def test_remove_extra_spaces(self):
        """测试去除多余空格"""
        text = "这是  一段   有多余空格  的文本"
        cleaned = _clean_text(text)
        assert "   " not in cleaned
        assert "这是 一段 有多余空格 的文本" == cleaned

    def test_remove_excessive_newlines(self):
        """测试去除过多连续换行"""
        text = "第一行\n\n\n\n\n第二行"
        cleaned = _clean_text(text)
        assert cleaned.count("\n") <= 2  # 最多保留2个连续换行

    def test_strip_lines(self):
        """测试去除行首行尾空白"""
        text = "  行1  \n  行2  \n  行3  "
        cleaned = _clean_text(text)
        lines = cleaned.split("\n")
        for line in lines:
            assert line == line.strip()

    def test_remove_zero_width_space(self):
        """测试去除零宽空格"""
        text = "产品​规格​说明"
        cleaned = _clean_text(text)
        assert "​" not in cleaned
        assert "产品规格说明" == cleaned

    def test_preserve_single_newline(self):
        """测试保留单个换行"""
        text = "第一行\n第二行\n第三行"
        cleaned = _clean_text(text)
        assert cleaned.count("\n") == 2

    def test_preserve_double_newline(self):
        """测试保留段落分隔（两个换行）"""
        text = "段落1\n\n段落2\n\n段落3"
        cleaned = _clean_text(text)
        assert "\n\n" in cleaned
        assert cleaned.count("\n\n") == 2

    def test_empty_text(self):
        """测试空文本"""
        assert _clean_text("") == ""
        assert _clean_text("   \n\n  ") == ""

    def test_mixed_whitespace(self):
        """测试混合空白字符"""
        text = "  \t  行1  \t \n  \t  行2  "
        cleaned = _clean_text(text)
        assert "\t" not in cleaned
        assert "行1" in cleaned
        assert "行2" in cleaned

    def test_chinese_punctuation_normalization(self):
        """测试中文文本不受影响"""
        text = "iPhone 15 Pro Max 电池容量为 4422mAh，支持 27W 快充。\n华为 Mate 60 Pro 电池容量为 4750mAh。"
        cleaned = _clean_text(text)
        assert "iPhone 15 Pro Max" in cleaned
        assert "华为 Mate 60 Pro" in cleaned
        assert "4422mAh" in cleaned
        assert "4750mAh" in cleaned


class TestFriendlyError:
    """验收 A9：底层错误中文映射（技术原文保留）"""

    def test_pdf_startxref_translated(self):
        msg = _friendly_error("startxref not found")
        assert "不是有效的 PDF" in msg
        assert "startxref not found" in msg  # 技术原文保留排障

    def test_pdf_encrypted_translated(self):
        msg = _friendly_error("Invalid password")
        assert "已加密" in msg

    def test_pdf_encrypted_not_decrypted_translated(self):
        """批次F 验收修复：pypdf 对加密 PDF 抛"File has not been decrypted"
        （非"Invalid password"）——此前显示英文原文，加密文档上传失败
        没有中文提示"""
        msg = _friendly_error("File has not been decrypted")
        assert "已加密" in msg
        assert "解除密码" in msg

    def test_unknown_error_passthrough(self):
        assert _friendly_error("some random error") == "some random error"


class TestScannedPdfOcrFallback:
    """批次F 验收修复：纯图片型 PDF 文字提取为空——空文本检查此前在
    OCR 分流之前 return，扫描件永远走不到 OCR 就报"文档内容为空"
    （真实验收：扫描版 PDF 上传失败）。修复：PDF 先试 OCR，OCR 也
    空才报错；非 PDF 空文本行为不变。"""

    def _patch_pipeline(self, monkeypatch, load_result, ocr_result):
        import app.pipeline.processor as pp

        monkeypatch.setattr(pp, "load_document", lambda path: load_result)
        # ocr_pdf_file 在 processor 内惰性 import——须打到 app.rag.ocr 源头
        import app.rag.ocr as ocr_mod

        monkeypatch.setattr(ocr_mod, "ocr_pdf_file", lambda path: ocr_result)
        # 分块与入库 mock：只要走到分块即算成功路径
        from langchain_core.documents import Document

        monkeypatch.setattr(
            pp, "chunk_by_strategy",
            lambda text, ct, md: [Document(page_content="块", metadata={})],
        )
        monkeypatch.setattr(pp, "add_documents_to_store", lambda chunks: None)

    def test_empty_pdf_falls_back_to_ocr(self, monkeypatch, tmp_path):
        """空文本 PDF → OCR 有结果 → 处理成功（修复前报"文档内容为空"）"""
        self._patch_pipeline(monkeypatch, load_result="", ocr_result="访客登记制度全文")
        f = tmp_path / "scan.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        n, err = process_document("d1", str(f), "扫描件.pdf")
        assert n == 1 and err is None

    def test_empty_pdf_ocr_empty_still_errors(self, monkeypatch, tmp_path):
        """空文本 PDF → OCR 也空 → 仍报"文档内容为空"（降级不静默成功）"""
        self._patch_pipeline(monkeypatch, load_result="", ocr_result="")
        f = tmp_path / "scan.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        n, err = process_document("d2", str(f), "扫描件.pdf")
        assert n == 0 and "文档内容为空" in (err or "")

    def test_empty_txt_still_errors_without_ocr(self, monkeypatch, tmp_path):
        """非 PDF 空文本不触发 OCR（行为不变）"""
        self._patch_pipeline(monkeypatch, load_result="", ocr_result="不该被调用")
        f = tmp_path / "e.txt"
        f.write_text("", encoding="utf-8")
        n, err = process_document("d3", str(f), "空文档.txt")
        assert n == 0 and "文档内容为空" in (err or "")

    def test_text_pdf_skips_ocr(self, monkeypatch, tmp_path):
        """文字版 PDF（提取文本充足）不触发 OCR——避免 OCR 误开销"""
        self._patch_pipeline(monkeypatch,
                             load_result="访客登记管理制度全文" * 10,
                             ocr_result="不该被调用")
        f = tmp_path / "text.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        n, err = process_document("d4", str(f), "文字版.pdf")
        assert n == 1 and err is None
