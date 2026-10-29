"""文档相关 Schema"""
from datetime import datetime
from pydantic import BaseModel, Field


class DocumentResponse(BaseModel):
    id: str
    filename: str
    original_name: str
    file_type: str
    file_size: int
    content_type: str = "long_text"
    status: str
    chunk_count: int
    kb_id: str | None = None
    kb_name: str | None = None
    category_id: str | None = None
    category_name: str | None = None
    created_at: datetime
    updated_at: datetime
    # 解析事件流水（产品化 A3）：[{"time","stage","detail"}]；旧数据无此列时为 None
    process_log: list[dict[str, str]] | None = None
    # 失败原因（解析流水弹窗顶部醒目标注用；成功时为空）
    error_message: str | None = None
    # 批次5 7b 版本管理：版本号（1=首版）与上一版本 id（None=首版）
    version_no: int = 1
    version_of: str | None = None

    model_config = {"from_attributes": True}


class DocumentUploadResponse(BaseModel):
    id: str
    filename: str
    status: str
    message: str


class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    parent_id: str | None = None
    kb_id: str | None = None  # 目标知识库（企业知识库阶段1：分类归属知识库）


class CategoryResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    parent_id: str | None = None
    kb_id: str | None = None
    children: list["CategoryResponse"] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class ChunkResponse(BaseModel):
    """单个分块信息"""
    chunk_id: str
    content: str
    chunk_index: int
    chunk_hash: str
    filename: str
    category_id: str
    parent_category_id: str = ""
    chunk_type: str = ""  # 分块模式：structured / faq / long_text / default


class ChunkListResponse(BaseModel):
    """文档分块列表"""
    document_id: str
    total: int
    chunks: list[ChunkResponse]


class KnowledgeStats(BaseModel):
    total_documents: int
    total_chunks: int
    completed_documents: int
    processing_documents: int
    failed_documents: int
    total_size_bytes: int
    categories_count: int
    category_document_counts: dict[str, int] = {}
    kb_id: str | None = None  # 指定了 kb 参数时为该库统计，None 为全局
