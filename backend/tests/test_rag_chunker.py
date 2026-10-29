"""RAG 分块策略测试 — 纯单元测试，无需数据库"""
import pytest
from langchain_core.documents import Document

from app.rag.chunker import (
    chunk_faq,
    chunk_structured,
    chunk_long_text,
    chunk_by_strategy,
)


# ════════════════════════════════════════════════════
# chunk_faq 测试 — 按双换行切分，chunk_size=200, overlap=20
# ════════════════════════════════════════════════════

class TestChunkFAQ:
    """FAQ 分块策略测试（按 \\n\\n 切分）"""

    def test_faq_split_by_double_newline(self):
        """测试按双换行切分"""
        text = (
            "Q: 你们支持哪些支付方式？\nA: 我们支持微信支付、支付宝、银行卡转账。\n\n"
            "Q: 如何申请退款？\nA: 请在订单页面点击申请退款，客服将在24小时内处理。\n"
        )
        base_meta = {"source": "faq_doc.txt"}
        chunks = chunk_faq(text, base_meta)

        assert len(chunks) >= 1
        assert all(c.metadata["chunk_type"] == "faq" for c in chunks)

    def test_faq_with_chinese_markers(self):
        """测试中文问/答格式按双换行切分"""
        text = (
            "问：产品保修期多久？\n答：所有产品享有一年免费保修服务。\n\n"
            "问：可以延保吗？\n答：可以，延保费用为产品价格的10%/年。\n"
        )
        base_meta = {"source": "warranty.txt"}
        chunks = chunk_faq(text, base_meta)

        assert len(chunks) >= 1
        assert all(c.metadata["chunk_type"] == "faq" for c in chunks)
        assert "一年免费保修" in "".join(c.page_content for c in chunks)

    def test_faq_with_mixed_formats(self):
        """测试混合格式按双换行切分"""
        text = (
            "Q: 发货时间是多久？\nA: 下单后48小时内发货。\n\n"
            "Q: 支持货到付款吗？\nA: 目前暂不支持货到付款。\n"
        )
        base_meta = {"source": "shipping.txt"}
        chunks = chunk_faq(text, base_meta)

        assert len(chunks) >= 1

    def test_faq_with_markdown_bold_format(self):
        """测试粗体格式FAQ"""
        text = "**Q: 发货时间是多久？**\n**A: 下单后48小时内发货。**\n"
        base_meta = {"source": "bold_faq.txt"}
        chunks = chunk_faq(text, base_meta)

        assert isinstance(chunks, list)
        assert len(chunks) >= 1

    def test_faq_double_newline_split(self):
        """测试文本按双换行拆分为多个chunk"""
        text = (
            "## 第一节：概述\n这是概述内容。\n\n"
            "## 第二节：详细说明\n这是详细说明内容。\n"
        )
        base_meta = {"source": "no_qa.txt"}
        chunks = chunk_faq(text, base_meta)

        assert len(chunks) == 1  # 短文本，一块
        assert chunks[0].metadata["chunk_type"] == "faq"

    def test_faq_empty_text(self):
        """测试空文本"""
        chunks = chunk_faq("", {"source": "empty.txt"})
        assert len(chunks) == 0

    def test_faq_whitespace_only(self):
        """测试纯空白文本"""
        chunks = chunk_faq("   \n\n  ", {"source": "blank.txt"})
        assert len(chunks) == 0

    def test_faq_separator_double_newline(self):
        """测试用双换行分隔的FAQ条目"""
        text = "Q: 问题1？\nA: 答案1\n\nQ: 问题2？\nA: 答案2\n"
        base_meta = {"source": "sep.txt"}
        chunks = chunk_faq(text, base_meta)

        assert len(chunks) >= 1

    def test_faq_metadata_preservation(self):
        """测试 base_metadata 被正确合并"""
        text = "Q: 测试问题？\nA: 测试答案。\n"
        base_meta = {"source": "test.txt", "category_id": "cat_001"}
        chunks = chunk_faq(text, base_meta)

        assert len(chunks) == 1
        assert chunks[0].metadata["source"] == "test.txt"
        assert chunks[0].metadata["category_id"] == "cat_001"
        assert chunks[0].metadata["chunk_type"] == "faq"


# ════════════════════════════════════════════════════
# chunk_structured 测试 — 不分块，整个文档一块
# ════════════════════════════════════════════════════

class TestChunkStructured:
    """结构化数据分块测试（不分块，整个文档一块）"""

    def test_structured_single_chunk(self):
        """测试整个文档作为一个chunk"""
        text = "产品名称,价格,库存\n手机A,2999,100\n手机B,3999,50\n"
        base_meta = {"source": "products.csv"}
        chunks = chunk_structured(text, base_meta)

        assert len(chunks) == 1
        assert chunks[0].metadata["chunk_type"] == "structured"
        assert "手机A" in chunks[0].page_content
        assert "手机B" in chunks[0].page_content

    def test_structured_metadata_inheritance(self):
        """测试 metadata 继承（不再逐行展开列值）"""
        text = "名称,品牌,价格\n产品X,BrandA,199\n"
        base_meta = {"source": "test.csv", "document_id": "doc_1"}
        chunks = chunk_structured(text, base_meta)

        assert len(chunks) == 1
        assert chunks[0].metadata["chunk_type"] == "structured"
        assert chunks[0].metadata["source"] == "test.csv"
        assert chunks[0].metadata["document_id"] == "doc_1"

    def test_structured_empty_content(self):
        """测试空文本"""
        chunks = chunk_structured("", {"source": "empty.csv"})
        assert len(chunks) == 0

    def test_structured_whitespace_only(self):
        """测试纯空白"""
        chunks = chunk_structured("   \n  ", {"source": "blank.csv"})
        assert len(chunks) == 0

    def test_structured_with_empty_fields(self):
        """测试含空字段的文档仍然一整块"""
        text = "名称,价格,备注\n产品A,100,\n产品B,,缺货\n"
        base_meta = {"source": "partial.csv"}
        chunks = chunk_structured(text, base_meta)

        assert len(chunks) == 1
        assert "产品A" in chunks[0].page_content
        assert "产品B" in chunks[0].page_content

    def test_structured_special_characters(self):
        """测试特殊字符"""
        text = '问题,回答\n"你好"是什么意思？,"你好"是问候语\n'
        base_meta = {"source": "special.csv"}
        chunks = chunk_structured(text, base_meta)

        assert len(chunks) == 1


# ════════════════════════════════════════════════════
# chunk_long_text 测试 — chunk_size=600, overlap=100
# ════════════════════════════════════════════════════

class TestChunkLongText:
    """长文本分块测试"""

    def test_basic_long_text_chunking(self, monkeypatch):
        """测试基本长文本分块（验收批次1·1c：厚文档自动子分块，产出子块）"""
        monkeypatch.setattr("app.config.settings.retrieval_subchunk_enabled", True)
        monkeypatch.setattr("app.config.settings.retrieval_subchunk_min_chars", 0)
        text = "这是一段很长的文本。" * 200  # 约 2000 字
        base_meta = {"source": "long.txt"}
        chunks = chunk_long_text(text, base_meta)

        assert len(chunks) >= 1
        assert all(isinstance(c, Document) for c in chunks)
        # 长文本 → 子块：小块匹配（≤240 字）、携带父块全文
        assert all(c.metadata["chunk_type"] == "sub_chunk" for c in chunks)
        assert all(c.metadata.get("parent_content") for c in chunks)
        assert all(c.metadata.get("parent_id") for c in chunks)
        for c in chunks:
            assert len(c.page_content) <= 300

    def test_short_text_single_chunk(self):
        """测试短文本只产生一个 chunk"""
        text = "这是一段短文本。"
        base_meta = {"source": "short.txt"}
        chunks = chunk_long_text(text, base_meta)

        assert len(chunks) == 1
        assert chunks[0].page_content == text

    def test_empty_long_text(self):
        """测试空文本"""
        chunks = chunk_long_text("", {"source": "empty.txt"})
        assert len(chunks) == 0

    def test_chunk_with_chinese_punctuation(self):
        """测试中文标点分句"""
        text = "第一句话。第二句话！第三句话？第四句话；第五句话，第六句话、第七个词 空格分割"
        base_meta = {"source": "cn.txt"}
        chunks = chunk_long_text(text, base_meta)

        assert len(chunks) == 1  # 不够长，一个 chunk
        assert text in chunks[0].page_content

    def test_chunk_metadata_inheritance(self):
        """测试 metadata 继承"""
        text = "测试内容" * 100
        base_meta = {"document_id": "doc_123", "filename": "test.txt"}
        chunks = chunk_long_text(text, base_meta)

        for chunk in chunks:
            assert chunk.metadata["document_id"] == "doc_123"
            assert chunk.metadata["filename"] == "test.txt"
            assert "chunk_type" in chunk.metadata


# ════════════════════════════════════════════════════
# chunk_by_strategy 路由器测试
# ════════════════════════════════════════════════════

class TestChunkByStrategy:
    """策略路由器测试"""

    def test_faq_strategy_routing(self):
        """测试 faq 策略路由"""
        text = "Q: 测试？\nA: 答案。\n"
        meta = {"source": "t.txt"}
        chunks = chunk_by_strategy(text, "faq", meta)
        assert len(chunks) == 1
        assert chunks[0].metadata["chunk_type"] == "faq"

    def test_structured_strategy_routing(self):
        """测试 structured 策略路由"""
        text = "col1,col2\nval1,val2\n"
        meta = {"source": "t.csv"}
        chunks = chunk_by_strategy(text, "structured", meta)
        assert len(chunks) == 1
        assert chunks[0].metadata["chunk_type"] == "structured"

    def test_long_text_strategy_routing(self):
        """测试 long_text 策略路由（开关默认关 → 父块直出，与旧行为一致）"""
        text = "普通文本内容。" * 50
        meta = {"source": "t.txt"}
        chunks = chunk_by_strategy(text, "long_text", meta)
        assert len(chunks) >= 1
        assert chunks[0].metadata["chunk_type"] == "long_text"

    def test_chat_log_strategy_routing(self):
        """测试 chat_log 策略路由到 default"""
        text = "用户: 你好\n客服: 您好\n" * 50
        meta = {"source": "chat.log"}
        chunks = chunk_by_strategy(text, "chat_log", meta)
        assert len(chunks) >= 1
        assert chunks[0].metadata["chunk_type"] == "default"

    def test_unknown_strategy_fallback(self):
        """测试未知策略回退到 default"""
        text = "任意内容" * 50
        meta = {"source": "t.txt"}
        chunks = chunk_by_strategy(text, "nonexistent_strategy", meta)
        assert len(chunks) >= 1
        assert chunks[0].metadata["chunk_type"] == "default"

    def test_strategy_with_category_metadata(self):
        """测试分类 metadata 传递"""
        text = "Q: 问题？\nA: 答案。\n"
        meta = {"document_id": "d1", "filename": "f.txt", "category_id": "cat_1"}
        chunks = chunk_by_strategy(text, "faq", meta)

        for chunk in chunks:
            assert chunk.metadata["document_id"] == "d1"
            assert chunk.metadata["category_id"] == "cat_1"
