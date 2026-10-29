"""审批链规则模型（批次2-4 规则腿）——文档解析产物，确定性匹配数据源"""
from datetime import datetime

from sqlalchemy import DateTime, Float, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base


class ApprovalRule(Base):
    """一条审批链规则（报销/采购/请假 的金额或天数区间 → 审批人/机构）"""

    __tablename__ = "approval_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    business_type: Mapped[str] = mapped_column(String(20), index=True)  # 报销/采购/请假
    min_value: Mapped[float | None] = mapped_column(Float, nullable=True)  # 区间下限
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)  # 上限（None=封顶）
    unit: Mapped[str] = mapped_column(String(10), default="元")  # 元/天
    approver: Mapped[str] = mapped_column(String(50))  # 审批人/机构（原文文本）
    action: Mapped[str] = mapped_column(String(10), default="审批")  # 审批/备案
    source_doc_id: Mapped[str] = mapped_column(String(36))  # 证据溯源
    source_text: Mapped[str] = mapped_column(Text)  # 规则原文片段
    # 批次3：制度生效日期（文本解析，无日期句式则为 None——裁决回退上传时间）
    effective_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
