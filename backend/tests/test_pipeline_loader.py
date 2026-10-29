"""文档加载器测试 — 纯单元测试"""
import os
import tempfile
import pytest
from pathlib import Path

from app.pipeline.loader import (
    validate_file,
    load_document,
    ALLOWED_MIME,
)


# ════════════════════════════════════════════════════
# validate_file 测试
# ════════════════════════════════════════════════════

class TestValidateFile:
    """文件校验测试"""

    def test_validate_txt_file(self):
        """测试 TXT 文件校验通过"""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write("Hello World".encode("utf-8"))
            tmp_path = f.name

        try:
            assert validate_file(tmp_path) is True
        finally:
            os.unlink(tmp_path)

    def test_validate_md_file(self):
        """测试 MD 文件校验通过"""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            f.write("# Title\nContent".encode("utf-8"))
            tmp_path = f.name

        try:
            assert validate_file(tmp_path) is True
        finally:
            os.unlink(tmp_path)

    def test_validate_csv_file(self):
        """测试 CSV 文件校验通过"""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write("col1,col2\nval1,val2".encode("utf-8"))
            tmp_path = f.name

        try:
            assert validate_file(tmp_path) is True
        finally:
            os.unlink(tmp_path)

    def test_validate_pdf_file(self):
        """测试 PDF 文件魔数校验"""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3")
            tmp_path = f.name

        try:
            assert validate_file(tmp_path) is True
        finally:
            os.unlink(tmp_path)

    def test_validate_docx_file(self):
        """测试 DOCX 文件魔数校验"""
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
            f.write(b"PK\x03\x04" + b"\x00" * 4)
            tmp_path = f.name

        try:
            assert validate_file(tmp_path) is True
        finally:
            os.unlink(tmp_path)

    def test_validate_unsupported_extension(self):
        """测试不支持的扩展名"""
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            f.write(b"fake content")
            tmp_path = f.name

        try:
            assert validate_file(tmp_path) is False
        finally:
            os.unlink(tmp_path)

    def test_validate_fake_pdf(self):
        """测试伪造成 PDF 的非 PDF 文件"""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"This is not a PDF file")
            tmp_path = f.name

        try:
            assert validate_file(tmp_path) is False
        finally:
            os.unlink(tmp_path)

    def test_validate_fake_docx(self):
        """测试伪造成 DOCX 的非 DOCX 文件"""
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
            f.write(b"This is not a DOCX file")
            tmp_path = f.name

        try:
            assert validate_file(tmp_path) is False
        finally:
            os.unlink(tmp_path)

    def test_validate_nonexistent_file(self):
        """测试不存在的文件"""
        assert validate_file("/tmp/nonexistent_file_xyz.txt") is False

    def test_allowed_mime_coverage(self):
        """测试 ALLOWED_MIME 包含所有支持的类型"""
        assert ".txt" in ALLOWED_MIME
        assert ".md" in ALLOWED_MIME
        assert ".pdf" in ALLOWED_MIME
        assert ".docx" in ALLOWED_MIME
        assert ".csv" in ALLOWED_MIME
        assert ".xlsx" in ALLOWED_MIME  # 产品化 A1：表格模板新增
        # 批次D·11d：图片文档（picture 模板）
        assert ".png" in ALLOWED_MIME
        assert ".jpg" in ALLOWED_MIME
        assert ".jpeg" in ALLOWED_MIME
        assert len(ALLOWED_MIME) == 9


# ════════════════════════════════════════════════════
# load_document 测试
# ════════════════════════════════════════════════════

class TestLoadDocument:
    """文档加载测试"""

    def test_load_txt_file(self):
        """测试加载 TXT 文件"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write("这是测试内容\n第二行")
            tmp_path = f.name

        try:
            content = load_document(tmp_path)
            assert "这是测试内容" in content
            assert "第二行" in content
        finally:
            os.unlink(tmp_path)

    def test_load_md_file(self):
        """测试加载 Markdown 文件"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write("# 标题\n\n正文内容")
            tmp_path = f.name

        try:
            content = load_document(tmp_path)
            assert "# 标题" in content
            assert "正文内容" in content
        finally:
            os.unlink(tmp_path)

    def test_load_csv_file(self):
        """测试加载 CSV 文件"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            f.write("名称,价格\n产品A,100\n产品B,200")
            tmp_path = f.name

        try:
            content = load_document(tmp_path)
            assert "名称" in content
            assert "产品A" in content
            assert "产品B" in content
        finally:
            os.unlink(tmp_path)

    def test_load_unsupported_extension(self):
        """测试不支持的扩展名抛出异常"""
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            f.write(b"fake")
            tmp_path = f.name

        try:
            with pytest.raises(ValueError, match="不支持的文件格式"):
                load_document(tmp_path)
        finally:
            os.unlink(tmp_path)

    def test_load_pdf_file(self):
        """测试加载 PDF 文件"""
        # 创建一个最小的有效 PDF
        minimal_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<<>>>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF"
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(minimal_pdf)
            tmp_path = f.name

        try:
            content = load_document(tmp_path)
            # PDF 可能没有文本内容，但不应抛出异常
            assert isinstance(content, str)
        finally:
            os.unlink(tmp_path)

    def test_load_empty_txt(self):
        """测试加载空 TXT 文件"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write("")
            tmp_path = f.name

        try:
            content = load_document(tmp_path)
            assert content == ""
        finally:
            os.unlink(tmp_path)

    def test_load_txt_with_unicode(self):
        """测试加载包含 Unicode 的 TXT 文件"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write("iPhone 15 Pro Max 📱 — 电池容量 4422mAh")
            tmp_path = f.name

        try:
            content = load_document(tmp_path)
            assert "📱" in content
            assert "4422mAh" in content
        finally:
            os.unlink(tmp_path)
