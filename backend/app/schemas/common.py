"""通用 Schema"""
from pydantic import BaseModel


class PaginatedResponse(BaseModel):
    """分页响应"""
    items: list
    total: int
    page: int
    page_size: int


class MessageResponse(BaseModel):
    """通用消息响应"""
    message: str
