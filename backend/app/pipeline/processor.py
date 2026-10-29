"""文档处理管道 - 编排加载→分块→向量化→存储全流程"""
import logging
import os
import hashlib
from pathlib import Path

logger = logging.getLogger(__name__)

from langchain_core.documents import Document

from app.config import settings
from app.rag.chunker import chunk_by_strategy
from app.rag.retriever import add_documents_to_store
from app.pipeline.loader import load_document


def _compute_file_hash(file_path: str) -> str:
    """计算文件 MD5 哈希，用于去重"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def process_document(
    document_id: str,
    file_path: str,
    original_name: str,
    category_id: str | None = None,
    content_type: str = "long_text",
    parent_category_id: str | None = None,
) -> tuple[int, str | None]:
    """
    处理文档: 加载→分块→向量化→存储
    返回: (chunk数量, 错误信息或None)

    注意：本函数全程同步阻塞（文件 IO + 嵌入 API + Chroma 写入），
    调用方必须用 asyncio.to_thread 在线程池执行（见 document_service）。
    """
    try:
        base_metadata = {
            "document_id": document_id,
            "filename": original_name,
            "parent_category_id": parent_category_id or "",
            "category_id": category_id or "",
        }

        if content_type == "table":
            # 表格模板（产品化 A1）：结构化加载表头+行，按行组切分、列头注入每块。
            # 传入原始文件名（验收修复 A3）：磁盘文件名是 UUID，表名不能用它
            from app.pipeline.loader import load_table
            from app.rag.chunker import chunk_table

            sheets = load_table(file_path, fallback_name=original_name)
            if not sheets:
                return 0, "表格内容为空，无法解析"
            chunks = chunk_table(sheets, base_metadata)
        elif content_type == "picture":
            # 批次D·11d picture 模板：图片文档（PNG/JPG）——OCR 抠字 +
            # vision 看图说话合成入库文本（图片本身不可检索，描述是唯一载体）；
            # 两路任一失败静默降级（至少有一路文本才入库，全空报错）。
            # 批次D 后续：表格照片先做结构识别（V3 现成模型）——识别到
            # 表格时 Markdown 表格进【表格内容】段（结构可检索、AI 可读）
            from app.rag.ocr import ocr_image_raw
            from app.rag.picture import build_picture_text, describe_image

            ocr_text, ocr_raw = ocr_image_raw(Path(file_path).read_bytes())
            vision_desc = describe_image(file_path)
            table_md = ""
            if ocr_raw:
                # 批次D后续质检修复：PIL 读图加异常保护（截断图片会
                # 让整个 picture 任务失败——表格识别失败必须静默降级）
                try:
                    from PIL import Image

                    import numpy as np

                    from app.rag.table_ocr import recognize_table_markdown

                    # 批次D后续质检低危#6：with 管理图片句柄（异常路径同样关闭）
                    with Image.open(file_path) as im:
                        img_arr = np.array(im.convert("RGB"))
                    table_md = recognize_table_markdown(img_arr, ocr_raw)
                except Exception as e:
                    logger.warning("表格识别失败（降级纯 OCR 文本）: %s", e)
            if not ocr_text and not vision_desc and not table_md:
                # 批次D后续质检低危#5：报错文案补表格路（表格路不可能单独存活
                # ——table_md 非空必有 ocr_text——仅文案口径对齐）
                return 0, "图片内容无法识别（OCR、图像理解与表格识别均失败）"
            picture_text = build_picture_text(
                original_name, ocr_text, vision_desc,
                table_markdown=table_md,
            )
            # 合成文本 500~1200 字——整体一块入库（描述+文字一体，切分反而碎语义）
            from app.rag.chunker import chunk_structured

            chunks = chunk_structured(picture_text, base_metadata)
        else:
            # 1. 加载文档
            raw_text = load_document(file_path)

            # 批次D·3a OCR 分流（不替换）：照片型 PDF（提取文本近乎为空）
            # 丢给 OCR 转规整文本——"损坏扫描件"从报错升级为正确解析；
            # OCR 不可用/识别为空时保持原样（静默降级，由后续逻辑处理）。
            # 质检 P2-2：page_count 传实际页数——阈值按"每页平均字符"判定
            # （不传会退化为"整篇 <50 字符"，多页扫描件带页码/印章杂文本
            # 会漏出分流）。
            # 批次F 验收修复：纯图片型 PDF 文字提取为空串，此前"空文本"
            # 检查在 OCR 分流之前 return——扫描件永远走不到 OCR 就报
            # "文档内容为空"（真实验收：扫描版 PDF 上传失败）。空检查
            # 后置：PDF 先试 OCR，OCR 也空才报错；非 PDF 空文本不变。
            if original_name.lower().endswith(".pdf"):
                from app.rag.ocr import is_scanned_pdf, ocr_pdf_file

                page_count = 1
                try:
                    import pymupdf

                    with pymupdf.open(file_path) as _doc:
                        page_count = _doc.page_count or 1
                except Exception as e:
                    # 批次E P3：页数获取失败留日志（按 1 页保守判定——宁可多 OCR 不漏）
                    logger.warning("PDF 页数获取失败（按 1 页保守判定）: %s", e)
                # 空文本直接按扫描件处理（is_scanned_pdf 对空串恒真，
                # 显式短路省一次判定且语义直白）
                if not raw_text.strip() or is_scanned_pdf(raw_text, page_count=page_count):
                    ocr_text = ocr_pdf_file(file_path)
                    if ocr_text and len(ocr_text) > len(raw_text):
                        raw_text = ocr_text

            if not raw_text or not raw_text.strip():
                return 0, "文档内容为空，无法提取文本"

            # 2. 文本清洗
            cleaned_text = _clean_text(raw_text)

            # 3. 分块
            chunks = chunk_by_strategy(cleaned_text, content_type, base_metadata)

        if not chunks:
            return 0, "文档分块后无有效内容"

        # 4. 为每个分块添加索引标记
        for i, chunk in enumerate(chunks):
            chunk.metadata["chunk_index"] = i
            chunk.metadata["chunk_hash"] = hashlib.md5(
                chunk.page_content.encode("utf-8")
            ).hexdigest()

        # 5. 写入 ChromaDB
        add_documents_to_store(chunks)

        return len(chunks), None

    except Exception as e:
        return 0, _friendly_error(str(e))


# 验收修复 A9：常见底层解析错误转中文提示（技术原文保留，便于排障）
_ERROR_TRANSLATIONS = [
    ("startxref not found", "文件不是有效的 PDF（结构已损坏），请重新导出后再上传"),
    ("EOF marker not found", "文件不是有效的 PDF（内容不完整），请重新导出后再上传"),
    ("Invalid password", "PDF 已加密，请解除密码保护后重新上传"),
    # 批次F 验收修复：pypdf 对加密 PDF 抛的是"未解密"而非"密码无效"——
    # 原映射表只覆盖后者，加密文档上传失败显示英文原文（真实验收）
    ("File has not been decrypted", "PDF 已加密，请解除密码保护后重新上传"),
    ("File is not a zip file", "文件不是有效的 Office 文档（docx/xlsx 是压缩容器格式），请重新保存后再上传"),
    ("not enough values to unpack", "CSV 表格列数不一致，请检查表头与数据行后再上传"),
]


def _friendly_error(message: str) -> str:
    """底层错误 → 中文可读提示；未命中映射表时原样返回"""
    for key, zh in _ERROR_TRANSLATIONS:
        if key in message:
            return f"{zh}（原始错误：{message[:100]}）"
    return message


def _clean_text(text: str) -> str:
    """文本清洗"""
    import re

    # 去除多余空白（保留单个换行）
    text = re.sub(r'[ \t]+', ' ', text)
    # 去除连续3个以上的换行
    text = re.sub(r'\n{3,}', '\n\n', text)
    # 去除行首行尾空白
    text = '\n'.join(line.strip() for line in text.split('\n'))
    # 规范化中文标点
    text = text.replace('​', '')  # 零宽空格

    return text.strip()
