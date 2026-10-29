"""产品化 A1 测试：表格模板全链路（loader 结构化加载 + processor 表格分支）"""
import pytest

from app.pipeline.loader import load_table, validate_file
from app.pipeline import processor


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "报销标准.csv"
    p.write_text(
        "项目,限额,说明\n市内交通,100元/天,凭发票报销\n住宿,300元/晚,一线城市\n",
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture
def xlsx_file(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "报销标准"
    ws.append(["项目", "限额", "说明"])
    ws.append(["市内交通", "100元/天", "凭发票报销"])
    ws.append(["住宿", "300元/晚", "一线城市"])
    p = tmp_path / "报销标准.xlsx"
    wb.save(str(p))
    return str(p)


class TestLoadTable:
    def test_load_table_csv(self, csv_file):
        """CSV 结构化加载：表头 + 数据行"""
        sheets = load_table(csv_file)
        assert len(sheets) == 1
        assert sheets[0]["name"] == "报销标准"
        assert sheets[0]["headers"] == ["项目", "限额", "说明"]
        assert len(sheets[0]["rows"]) == 2
        assert sheets[0]["rows"][0][0] == "市内交通"

    def test_load_table_xlsx(self, xlsx_file):
        """xlsx 结构化加载：sheet 名 + 表头 + 数据行"""
        sheets = load_table(xlsx_file)
        assert len(sheets) == 1
        assert sheets[0]["name"] == "报销标准"
        assert sheets[0]["headers"] == ["项目", "限额", "说明"]
        assert sheets[0]["rows"][1][1] == "300元/晚"

    def test_validate_xlsx(self, xlsx_file, tmp_path):
        """xlsx 魔数校验：真 zip 容器通过，伪造扩展名拒绝"""
        assert validate_file(xlsx_file) is True
        fake = tmp_path / "fake.xlsx"
        fake.write_bytes(b"not a zip file")
        assert validate_file(str(fake)) is False


class TestProcessorTableBranch:
    def test_process_table_template(self, csv_file, monkeypatch):
        """content_type=table 走结构化加载 + 表格分块，列头注入每块"""
        captured = []
        monkeypatch.setattr(
            processor, "add_documents_to_store",
            lambda chunks: captured.extend(chunks),
        )
        count, error = processor.process_document(
            "doc-table-1", csv_file, "报销标准.csv", content_type="table"
        )
        assert error is None
        assert count == 1
        chunk = captured[0]
        assert chunk.metadata["chunk_type"] == "table"
        assert chunk.metadata["table_header"] == "项目、限额、说明"
        assert "【表格：报销标准】列：项目、限额、说明" in chunk.page_content
        assert "市内交通" in chunk.page_content
        assert "100元/天" in chunk.page_content

    def test_process_table_uses_original_name_for_csv(self, tmp_path, monkeypatch):
        """验收回归 A3：CSV 表名用原始文件名而非 UUID 存储文件名"""
        captured = []
        monkeypatch.setattr(
            processor, "add_documents_to_store",
            lambda chunks: captured.extend(chunks),
        )
        # 模拟真实上传场景：磁盘文件名是 UUID（系统存储命名规则）
        uuid_path = tmp_path / "dc432e62-804c-4614-bd9c-6aa77cec848f.csv"
        uuid_path.write_text("项目,限额\n市内交通,100元/天\n", encoding="utf-8")
        count, error = processor.process_document(
            "doc-x", str(uuid_path), "请假类型对照表.csv", content_type="table"
        )
        assert error is None
        assert count == 1
        assert "【表格：请假类型对照表】" in captured[0].page_content
        assert "dc432e62" not in captured[0].page_content

    def test_process_table_empty(self, tmp_path, monkeypatch):
        """空表格返回明确错误"""
        monkeypatch.setattr(processor, "add_documents_to_store", lambda chunks: None)
        empty = tmp_path / "empty.csv"
        empty.write_text("", encoding="utf-8")
        count, error = processor.process_document(
            "doc-empty", str(empty), "empty.csv", content_type="table"
        )
        assert count == 0
        assert "表格内容为空" in (error or "")
