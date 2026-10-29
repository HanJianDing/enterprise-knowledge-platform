"""Embedding 模型工厂 - 使用阿里云百炼 DashScope"""
from langchain_community.embeddings import DashScopeEmbeddings
from app.config import settings


def get_embeddings() -> DashScopeEmbeddings:
    """获取百炼 Embedding 实例"""
    return DashScopeEmbeddings(
        model=settings.embedding_model,
        dashscope_api_key=settings.dashscope_api_key,
    )
