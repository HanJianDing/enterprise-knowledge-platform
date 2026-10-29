"""系统管理 API (需要 Admin 权限)"""
import csv
import asyncio
import io
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

from loguru import logger
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, or_

from app.api.deps import get_db, require_admin, require_audit_view
from app.utils.time import tz_today, tz_now
from app.models.user import User
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.feedback import MessageFeedback
from app.models.document import Document, KnowledgeCategory
from app.models.knowledge_base import Department, UserDepartment, KnowledgeBase, KbMember
from app.models.audit import AuditLog
from app.models.mcp_credential import McpCredential
from app.schemas.auth import (
    UserCreate, UserImportResponse, UserImportRowError, UserInfo,
    ResetPasswordRequest, validate_password_strength,
)
from app.schemas.chat import QuickQuestionsUpdate
from app.schemas.common import MessageResponse, PaginatedResponse
from app.schemas.kb import (
    KnowledgeBaseCreate, KnowledgeBaseUpdate, KnowledgeBaseResponse,
    KbMembersUpdate, KbMemberOut,
    DepartmentCreate, DepartmentUpdate, DepartmentResponse,
    UserDepartmentsUpdate,
)
from app.services import audit_service, auth_service, document_service, setting_service
from app.middleware.auth import hash_password
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

router = APIRouter(prefix="/api/v1/admin", tags=["系统管理"])


@router.get("/dashboard")
async def dashboard(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """系统仪表盘"""
    # 用户数
    result = await db.execute(select(func.count(User.id)))
    user_count = result.scalar() or 0

    # 会话数
    result = await db.execute(select(func.count(Conversation.id)))
    conv_count = result.scalar() or 0

    # 消息数
    result = await db.execute(select(func.count(Message.id)))
    msg_count = result.scalar() or 0

    # 文档统计
    result = await db.execute(select(func.count(Document.id)))
    doc_count = result.scalar() or 0

    result = await db.execute(
        select(Document.status, func.count(Document.id)).group_by(Document.status)
    )
    doc_status = {row[0]: row[1] for row in result.all()}

    return {
        "user_count": user_count,
        "conversation_count": conv_count,
        "message_count": msg_count,
        "document_count": doc_count,
        "documents_completed": doc_status.get("completed", 0),
        "documents_processing": doc_status.get("processing", 0),
        "documents_failed": doc_status.get("failed", 0),
    }


@router.get("/users", response_model=PaginatedResponse)
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="按用户名/邮箱模糊搜索"),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """用户列表（阶段3：支持 search 模糊搜索，供授权抽屉远程选人用）"""
    conds = []
    if search:
        conds.append(or_(
            User.username.contains(search), User.email.contains(search)
        ))
    result = await db.execute(select(func.count(User.id)).where(*conds))
    total = result.scalar() or 0

    result = await db.execute(
        select(User)
        .where(*conds)
        .order_by(desc(User.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    users = result.scalars().all()

    return PaginatedResponse(
        items=[UserInfo.model_validate(u) for u in users],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.patch("/users/{user_id}/role", response_model=MessageResponse)
async def change_user_role(
    user_id: str,
    role: str = Query(..., pattern="^(admin|editor|user)$"),
    request: Request = None,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """修改用户角色（admin | editor | user；editor=内容维护，可传文档不可管理库）"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return MessageResponse(message="用户不存在")
    if user.id == current_user.id:
        return MessageResponse(message="不能修改自己的角色")
    old_role = user.role
    user.role = role
    await db.flush()
    await audit_service.record_audit(db, current_user.id, current_user.username, "user.role_change", resource_type="user", resource_id=user_id,
        detail={"username": user.username, "from": old_role, "to": role},
        request=request,
    )
    return MessageResponse(message=f"用户角色已更新为 {role}")


@router.patch("/users/{user_id}/status", response_model=MessageResponse)
async def change_user_status(
    user_id: str,
    is_active: bool = Query(...),
    request: Request = None,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """启用/禁用用户"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return MessageResponse(message="用户不存在")
    if user.id == current_user.id:
        return MessageResponse(message="不能禁用自己")
    user.is_active = is_active
    await db.flush()
    await audit_service.record_audit(db, current_user.id, current_user.username, "user.status_change", resource_type="user", resource_id=user_id,
        detail={"username": user.username, "is_active": is_active},
        request=request,
    )
    return MessageResponse(message=f"用户已{'启用' if is_active else '禁用'}")


# ─────────────────────────── 建号/批量导入（阶段3：开放注册已关闭） ───────────────────────────

@router.post("/users", response_model=UserInfo, status_code=201)
async def create_user(
    data: UserCreate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员创建员工账号（初始密码首登强制修改）"""
    user = await auth_service.create_user(
        db, data.username, data.email, data.password, role=data.role
    )
    # 绑定部门（校验存在性，不存在的部门忽略并提示；输入去重防唯一约束 500）
    invalid_depts = []
    if data.department_ids:
        dept_ids = list(dict.fromkeys(data.department_ids))
        result = await db.execute(
            select(Department.id).where(Department.id.in_(dept_ids))
        )
        found = {row[0] for row in result.all()}
        invalid_depts = [d for d in dept_ids if d not in found]
        for dept_id in found:
            db.add(UserDepartment(user_id=user.id, department_id=dept_id))
        await db.flush()
    await audit_service.record_audit(db, current_user.id, current_user.username, "user.create", resource_type="user", resource_id=user.id,
        detail={"username": user.username, "role": user.role, "invalid_departments": invalid_depts},
        request=request,
    )
    return UserInfo.model_validate(user)


@router.post("/users/import", response_model=UserImportResponse)
async def import_users(
    file: UploadFile = File(...),
    request: Request = None,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """CSV 批量导入员工账号。

    CSV 列（表头必须一致）：用户名,邮箱,初始密码,角色(admin|editor|user,可空默认user),部门(名称,多个用|分隔,可空)
    每行独立处理：失败的行跳过并记录原因，不影响其余行。返回 (created, errors)。
    """
    # 审计者信息先快照：循环内的行级 db.rollback() 会让已加载的 User 对象
    # 过期（expired），之后再读 current_user.id 会触发数据库刷新而 MissingGreenlet
    auditor_id = current_user.id
    auditor_username = current_user.username

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV 文件编码必须是 UTF-8")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "用户名" not in reader.fieldnames:
        raise HTTPException(
            status_code=400,
            detail="CSV 表头必须包含: 用户名,邮箱,初始密码,角色,部门",
        )

    # 预载部门名→ID 映射（部门列按名称匹配，不存在记为行错误）
    dept_result = await db.execute(select(Department.id, Department.name))
    dept_name_to_id = {name: did for did, name in dept_result.all()}

    created = 0
    errors: list[UserImportRowError] = []
    for idx, row in enumerate(reader, start=2):  # 表头是第 1 行
        username = (row.get("用户名") or "").strip()
        email = (row.get("邮箱") or "").strip()
        password = (row.get("初始密码") or "").strip()
        role = (row.get("角色") or "").strip() or "user"
        # 部门名去重（重复输入会撞 user_departments 唯一约束）
        dept_names = list(dict.fromkeys(
            d.strip() for d in (row.get("部门") or "").split("|") if d.strip()
        ))

        def _err(reason: str) -> None:
            errors.append(UserImportRowError(row=idx, username=username or "(空)", reason=reason))

        if not username or not email or not password:
            _err("用户名/邮箱/初始密码不能为空")
            continue
        if role not in ("admin", "editor", "user", "auditor"):
            _err(f"非法角色: {role}")
            continue
        # 产品化 C2：CSV 导入的初始密码同样过密码策略（行级容错，弱密码单独报错）
        try:
            validate_password_strength(password)
        except ValueError as e:
            _err(str(e))
            continue
        dept_ids, bad_depts = [], []
        for name in dept_names:
            if name in dept_name_to_id:
                dept_ids.append(dept_name_to_id[name])
            else:
                bad_depts.append(name)
        if bad_depts:
            _err(f"部门不存在: {'、'.join(bad_depts)}")
            continue
        try:
            user = await auth_service.create_user(db, username, email, password, role=role)
        except HTTPException as e:
            _err(e.detail)
            await db.rollback()  # 行失败回滚后继续下一行（独立事务语义）
            continue
        for dept_id in dept_ids:
            db.add(UserDepartment(user_id=user.id, department_id=dept_id))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            _err("用户名或邮箱重复")
            continue
        created += 1

    await audit_service.record_audit(db, auditor_id, auditor_username, "user.import", detail={
            "created": created, "error_count": len(errors),
            "filename": file.filename,
        },
        request=request,
    )
    return UserImportResponse(created=created, errors=errors)


# ─────────────────────────── 审计日志（阶段3） ───────────────────────────

def _csv_safe(value: str) -> str:
    """CSV 注入防护：以 = + - @ 开头的单元格前缀单引号，Excel 才不会当公式执行。

    审计内容含用户可控文本（文件名/用户名），不防护的话恶意文件名
    （如 =cmd|...）会在导出后打开 CSV 时被 Excel 执行。
    """
    if value and value[0] in "=+-@\t":
        return "'" + value
    return value

@router.get("/audit-logs", response_model=PaginatedResponse)
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    action: str | None = Query(None),
    username: str | None = Query(None),
    resource_type: str | None = Query(None),
    current_user: User = Depends(require_audit_view),
    db: AsyncSession = Depends(get_db),
):
    """审计日志查询（按操作/用户名/资源类型过滤，时间倒序；admin/auditor 可见）"""
    conds = []
    if action:
        conds.append(AuditLog.action == action)
    if username:
        conds.append(AuditLog.username.contains(username))
    if resource_type:
        conds.append(AuditLog.resource_type == resource_type)

    result = await db.execute(select(func.count(AuditLog.id)).where(*conds))
    total = result.scalar() or 0

    result = await db.execute(
        select(AuditLog)
        .where(*conds)
        .order_by(desc(AuditLog.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    logs = result.scalars().all()
    return PaginatedResponse(
        items=[{
            "id": log.id,
            "username": log.username,
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "detail": log.detail,
            "ip": log.ip,
            "user_agent": log.user_agent,
            "created_at": log.created_at.isoformat(),
        } for log in logs],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/audit-logs/export")
async def export_audit_logs(
    action: str | None = Query(None),
    username: str | None = Query(None),
    resource_type: str | None = Query(None),
    current_user: User = Depends(require_audit_view),
    db: AsyncSession = Depends(get_db),
):
    """审计日志导出 CSV（产品化 C3：企业内审/回溯需要可导出的证据文件）。

    过滤参数与列表接口一致；CSV 带 UTF-8 BOM（Excel 直接打开中文不乱码）；
    详情字段 JSON 序列化后落入单元格。
    """
    import json

    from fastapi.responses import StreamingResponse

    conds = []
    if action:
        conds.append(AuditLog.action == action)
    if username:
        conds.append(AuditLog.username.contains(username))
    if resource_type:
        conds.append(AuditLog.resource_type == resource_type)

    result = await db.execute(
        select(AuditLog).where(*conds).order_by(desc(AuditLog.created_at)).limit(50000)
    )
    logs = result.scalars().all()

    # 行内容先行构建（一次性查询已限 5 万行）；逐行产出 CSV 文本
    def _gen():
        import csv as csv_mod

        yield "﻿"  # UTF-8 BOM
        header = ["时间", "用户", "操作", "资源类型", "资源ID", "详情", "IP", "User-Agent"]
        lines = [header]
        for log in logs:
            lines.append([
                log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else "",
                _csv_safe(log.username),
                _csv_safe(log.action),
                _csv_safe(log.resource_type or ""),
                _csv_safe(log.resource_id or ""),
                _csv_safe(json.dumps(log.detail, ensure_ascii=False) if log.detail else ""),
                _csv_safe(log.ip or ""),
                _csv_safe(log.user_agent or ""),
            ])
        # 逐行产出：writer 必须绑定当次缓冲区（绑定旧缓冲区会产出空串）
        for row in lines:
            buf = io.StringIO()
            csv_mod.writer(buf).writerow(row)
            yield buf.getvalue()

    return StreamingResponse(
        _gen(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="audit_logs.csv"',
        },
    )


@router.post("/users/{user_id}/reset-password", response_model=MessageResponse)
async def reset_user_password(
    user_id: str,
    data: ResetPasswordRequest,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员重置员工密码（产品化 C2：重置后首登强制改密 + 踢掉所有已登录会话）"""
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.role == "admin" and target.id != current_user.id:
        raise HTTPException(status_code=403, detail="不能重置其他管理员的密码")

    target.hashed_password = hash_password(data.new_password)
    target.must_change_password = True  # 首登强制改密（复用现有改密流程）
    target.session_version += 1         # 踢掉旧会话（与自主改密语义一致）
    await audit_service.record_audit(db, current_user.id, current_user.username, "user.password_reset",
        resource_type="user", resource_id=user_id,
        detail={"target_username": target.username}, request=request,
    )
    await db.commit()
    return MessageResponse(message=f"已重置 {target.username} 的密码（该账号下次登录需修改密码）")


@router.put("/settings/quick-questions", response_model=MessageResponse)
async def update_quick_questions(
    data: QuickQuestionsUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """改欢迎页快捷问题（1~8 条、每条 1~30 字，即时生效）"""
    cleaned = [q.strip() for q in data.questions]
    if not (1 <= len(cleaned) <= 8):
        raise HTTPException(status_code=400, detail="快捷问题需 1~8 条")
    for q in cleaned:
        if not q:
            raise HTTPException(status_code=400, detail="快捷问题不能为空")
        if len(q) > 30:
            raise HTTPException(status_code=400, detail="每条快捷问题不超过 30 字")
    await setting_service.set_quick_questions(db, cleaned)
    return MessageResponse(message="快捷问题已更新，即时生效")


# ─────────────────────────── 满意度反馈 ───────────────────────────

@router.get("/feedback/stats")
async def feedback_stats(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """满意度统计：赞/踩计数与占比（无评价时 satisfaction_rate 为 null）"""
    result = await db.execute(
        select(MessageFeedback.rating, func.count(MessageFeedback.id))
        .group_by(MessageFeedback.rating)
    )
    counts = {row[0]: row[1] for row in result.all()}
    like = counts.get("like", 0)
    dislike = counts.get("dislike", 0)
    total = like + dislike
    return {
        "like": like,
        "dislike": dislike,
        "total": total,
        "satisfaction_rate": round(like / total * 100, 1) if total else None,
    }


@router.get("/feedback")
async def feedback_list(
    rating: str | None = Query(None, description="like | dislike"),
    has_comment: bool | None = Query(None, description="只看填了/没填意见的"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """反馈明细（分页 + 过滤）：谁、在哪个会话、AI 怎么答的、评价与意见

    前端明细页在阶段 4 接入；本阶段先交付数据接口。
    """
    conditions = []
    if rating:
        conditions.append(MessageFeedback.rating == rating)
    if has_comment is not None:
        if has_comment:
            conditions.append(MessageFeedback.comment.is_not(None))
            conditions.append(MessageFeedback.comment != "")
        else:
            from sqlalchemy import or_
            conditions.append(
                or_(
                    MessageFeedback.comment.is_(None),
                    MessageFeedback.comment == "",
                )
            )

    total_result = await db.execute(
        select(func.count(MessageFeedback.id)).where(*conditions)
    )
    total = total_result.scalar() or 0

    result = await db.execute(
        select(MessageFeedback, Message, Conversation, User)
        .join(Message, Message.id == MessageFeedback.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(User, User.id == MessageFeedback.user_id)
        .where(*conditions)
        .order_by(desc(MessageFeedback.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = []
    for fb, msg, conv, user in result.all():
        items.append({
            "id": fb.id,
            "rating": fb.rating,
            "comment": fb.comment,
            "created_at": fb.created_at.isoformat(),
            "username": user.username,
            "conversation_title": conv.title,
            "message_content": (msg.content or "")[:100],
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ─────────────────────────── 图表看板 ───────────────────────────

@router.get("/dashboard/stats")
async def dashboard_stats(
    days: int = Query(30, ge=1, le=90, description="统计最近 N 天"),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """图表看板数据（一次返回）：

    - trend: 最近 days 天每天的新建会话数/消息数。SQLite 与 PG 双兼容：
      用 func.date() 按天分组（不用 generate_series/dialect cast），
      缺失日期在 Python 侧补零，保证前端折线连续。
    - feedback: 赞踩计数与满意度占比（复用阶段3评价数据）。
    - quality: 平均回答延迟(ms)与平均 token（assistant 且 latency>0 的完成消息）。
    - categories: 各分类文档数（未分类单列，按数量倒序，取前 10）。
    """
    since = tz_today() - timedelta(days=days - 1)
    since_dt = datetime.combine(since, time.min)

    # ── 会话/消息按天趋势 ──
    conv_result = await db.execute(
        select(func.date(Conversation.created_at).label("day"), func.count(Conversation.id))
        .where(Conversation.created_at >= since_dt)
        .group_by("day")
    )
    conv_by_day = {str(row[0])[:10]: row[1] for row in conv_result.all()}

    msg_result = await db.execute(
        select(func.date(Message.created_at).label("day"), func.count(Message.id))
        .where(Message.created_at >= since_dt)
        .group_by("day")
    )
    msg_by_day = {str(row[0])[:10]: row[1] for row in msg_result.all()}

    trend = []
    for i in range(days):
        d = since + timedelta(days=i)
        trend.append({
            "date": d.isoformat(),
            "conversations": conv_by_day.get(d.isoformat(), 0),
            "messages": msg_by_day.get(d.isoformat(), 0),
        })

    # ── 满意度 ──
    fb_result = await db.execute(
        select(MessageFeedback.rating, func.count(MessageFeedback.id))
        .group_by(MessageFeedback.rating)
    )
    counts = {row[0]: row[1] for row in fb_result.all()}
    like, dislike = counts.get("like", 0), counts.get("dislike", 0)
    fb_total = like + dislike

    # ── 消息质量（avg 在 SQLite 返回 float、PG 返回 Decimal，统一 float() 转换）──
    quality_result = await db.execute(
        select(func.avg(Message.latency_ms), func.avg(Message.token_count))
        .where(Message.role == "assistant", Message.latency_ms > 0)
    )
    avg_latency, avg_tokens = quality_result.one()

    # ── 分类文档分布（批次I：按主题父分类归并统计）──
    # 旧口径按文档挂的子分类统计：两层分类树下每个主题都有同名"制度/流程/表单/FAQ"
    # 子分类，图表横条名称大量重复（制度×3、表单×2……），与"知识库分类分布"
    # 标题名不副实。新口径归并到主题（父分类）：一条 = 一个主题的文档总数。
    doc_result = await db.execute(
        select(Document.category_id, func.count(Document.id))
        .group_by(Document.category_id)
    )
    doc_by_cat = {row[0]: row[1] for row in doc_result.all()}

    categories = await document_service.get_categories(db) or []
    cat_info: dict[str, dict] = {}  # 分类 id -> {name, parent_id}

    def walk(nodes: list) -> None:
        for c in nodes:
            cd = c.model_dump() if hasattr(c, "model_dump") else c
            cid = str(cd["id"])
            cat_info[cid] = {
                "name": str(cd["name"]),
                "parent_id": str(cd["parent_id"]) if cd.get("parent_id") else None,
            }
            walk(cd.get("children") or [])

    walk(categories)

    theme_counts: dict[str, int] = {}
    deleted_count = 0
    uncategorized = 0
    for cid, n in doc_by_cat.items():
        if not cid:
            uncategorized += n
            continue
        info = cat_info.get(cid)
        if info is None:
            deleted_count += n  # 分类已删除的文档归一条"已删除分类"
            continue
        # 文档挂子分类 → 归到其主题；文档直接挂父分类 → 归该父分类自身
        theme_id = info["parent_id"] or cid
        theme_counts[theme_id] = theme_counts.get(theme_id, 0) + n

    cat_dist = [
        {
            "category_id": tid,
            "name": cat_info[tid]["name"],
            "count": n,
        }
        for tid, n in theme_counts.items()
        if tid in cat_info  # 父分类已被删除的孤儿主题，并入"已删除分类"
    ]
    orphan_theme = sum(n for tid, n in theme_counts.items() if tid not in cat_info)
    if orphan_theme:
        deleted_count += orphan_theme
    if deleted_count:
        cat_dist.append({"category_id": None, "name": "已删除分类", "count": deleted_count})
    if uncategorized:
        cat_dist.append({"category_id": None, "name": "未分类", "count": uncategorized})
    cat_dist.sort(key=lambda x: x["count"], reverse=True)

    return {
        "trend": trend,
        "feedback": {
            "like": like,
            "dislike": dislike,
            "total": fb_total,
            "satisfaction_rate": round(like / fb_total * 100, 1) if fb_total else None,
        },
        "quality": {
            "avg_latency_ms": int(float(avg_latency)) if avg_latency is not None else None,
            "avg_tokens": round(float(avg_tokens), 1) if avg_tokens is not None else None,
        },
        "categories": cat_dist[:10],
    }


# ─────────────────────────── 知识库管理（企业知识库改造 阶段1） ───────────────────────────

@router.get("/knowledge-bases", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """知识库列表（含文档数与授权数）"""
    kb_result = await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.created_at))
    kbs = kb_result.scalars().all()

    doc_counts = dict(
        (await db.execute(
            select(Document.kb_id, func.count(Document.id)).group_by(Document.kb_id)
        )).all()
    )
    member_counts = dict(
        (await db.execute(
            select(KbMember.kb_id, func.count(KbMember.id)).group_by(KbMember.kb_id)
        )).all()
    )

    items = []
    for kb in kbs:
        resp = KnowledgeBaseResponse.model_validate(kb)
        resp.document_count = doc_counts.get(kb.id, 0)
        resp.member_count = member_counts.get(kb.id, 0)
        items.append(resp)
    return items


@router.post("/knowledge-bases", response_model=KnowledgeBaseResponse, status_code=201)
async def create_knowledge_base(
    data: KnowledgeBaseCreate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """创建知识库"""
    kb = KnowledgeBase(
        name=data.name,
        description=data.description,
        is_public=data.is_public,
        created_by=current_user.id,
    )
    db.add(kb)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="已存在同名知识库")
    await db.refresh(kb)
    await audit_service.record_audit(db, current_user.id, current_user.username, "kb.create", resource_type="knowledge_base", resource_id=kb.id,
        detail={"name": kb.name, "is_public": kb.is_public}, request=request,
    )
    resp = KnowledgeBaseResponse.model_validate(kb)
    resp.document_count = 0
    resp.member_count = 0
    return resp


@router.patch("/knowledge-bases/{kb_id}", response_model=MessageResponse)
async def update_knowledge_base(
    kb_id: str,
    data: KnowledgeBaseUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """修改知识库名称/描述/是否公共"""
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if data.name is not None:
        kb.name = data.name
    if data.description is not None:
        kb.description = data.description
    if data.is_public is not None:
        kb.is_public = data.is_public
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="已存在同名知识库")
    await audit_service.record_audit(db, current_user.id, current_user.username, "kb.update", resource_type="knowledge_base", resource_id=kb_id,
        detail=data.model_dump(exclude_none=True), request=request,
    )
    return MessageResponse(message="知识库已更新")


@router.delete("/knowledge-bases/{kb_id}", response_model=MessageResponse)
async def delete_knowledge_base(
    kb_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除知识库（库内还有文档时拒绝，防止误删——必须先清空文档）"""
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    doc_result = await db.execute(
        select(func.count(Document.id)).where(Document.kb_id == kb_id)
    )
    if (doc_result.scalar() or 0) > 0:
        raise HTTPException(
            status_code=400,
            detail="该知识库还有文档，请先删除库内文档或把文档移到其他库",
        )

    await db.execute(delete(KbMember).where(KbMember.kb_id == kb_id))
    await db.execute(delete(KnowledgeCategory).where(KnowledgeCategory.kb_id == kb_id))
    await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    await db.flush()
    await audit_service.record_audit(db, current_user.id, current_user.username, "kb.delete", resource_type="knowledge_base", resource_id=kb_id,
        detail={"name": kb.name}, request=request,
    )
    return MessageResponse(message="知识库已删除")


@router.get("/knowledge-bases/{kb_id}/members", response_model=list[KbMemberOut])
async def list_kb_members(
    kb_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """知识库授权列表（部门授权 + 个人授权，附主体名称）"""
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="知识库不存在")

    result = await db.execute(
        select(KbMember).where(KbMember.kb_id == kb_id).order_by(KbMember.created_at)
    )
    grants = result.scalars().all()

    dept_ids = [g.subject_id for g in grants if g.subject_type == "dept"]
    user_ids = [g.subject_id for g in grants if g.subject_type == "user"]
    dept_names, user_names = {}, {}
    if dept_ids:
        r = await db.execute(select(Department.id, Department.name).where(Department.id.in_(dept_ids)))
        dept_names = dict(r.all())
    if user_ids:
        r = await db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
        user_names = dict(r.all())

    return [
        KbMemberOut(
            id=g.id,
            subject_type=g.subject_type,
            subject_id=g.subject_id,
            subject_name=(
                dept_names.get(g.subject_id, "（部门已删除）")
                if g.subject_type == "dept"
                else user_names.get(g.subject_id, "（用户已删除）")
            ),
            can_read=g.can_read,
            can_upload=g.can_upload,
            can_manage=g.can_manage,
            created_at=g.created_at,
        )
        for g in grants
    ]


@router.put("/knowledge-bases/{kb_id}/members", response_model=MessageResponse)
async def update_kb_members(
    kb_id: str,
    data: KbMembersUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """整体替换知识库授权（PUT 语义：以提交列表为准，旧授权全部删除）"""
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 校验所有授权主体存在
    dept_ids = [m.subject_id for m in data.members if m.subject_type == "dept"]
    user_ids = [m.subject_id for m in data.members if m.subject_type == "user"]
    if dept_ids:
        r = await db.execute(select(Department.id).where(Department.id.in_(dept_ids)))
        found = {row[0] for row in r.all()}
        missing = [d for d in set(dept_ids) if d not in found]
        if missing:
            raise HTTPException(status_code=400, detail=f"部门不存在: {', '.join(missing)}")
    if user_ids:
        r = await db.execute(select(User.id).where(User.id.in_(user_ids)))
        found = {row[0] for row in r.all()}
        missing = [u for u in set(user_ids) if u not in found]
        if missing:
            raise HTTPException(status_code=400, detail=f"用户不存在: {', '.join(missing)}")

    await db.execute(delete(KbMember).where(KbMember.kb_id == kb_id))
    for m in data.members:
        db.add(KbMember(
            kb_id=kb_id,
            subject_type=m.subject_type,
            subject_id=m.subject_id,
            can_read=m.can_read,
            can_upload=m.can_upload,
            can_manage=m.can_manage,
        ))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=400, detail="授权列表存在重复项（同一部门/用户只能授权一次）"
        )
    await audit_service.record_audit(db, current_user.id, current_user.username, "kb.members_update", resource_type="knowledge_base", resource_id=kb_id,
        detail={"member_count": len(data.members)}, request=request,
    )
    return MessageResponse(message="知识库授权已更新")


# ─────────────────────────── 部门管理（企业知识库改造 阶段1） ───────────────────────────

@router.get("/departments", response_model=list[DepartmentResponse])
async def list_departments(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """部门树"""
    result = await db.execute(select(Department).order_by(Department.created_at))
    departments = result.scalars().all()

    dept_map: dict[str, DepartmentResponse] = {}
    roots: list[DepartmentResponse] = []
    for d in departments:
        dept_map[d.id] = DepartmentResponse(
            id=d.id, name=d.name, parent_id=d.parent_id,
            children=[], created_at=d.created_at,
        )
    for d in departments:
        resp = dept_map[d.id]
        if resp.parent_id and resp.parent_id in dept_map:
            dept_map[resp.parent_id].children.append(resp)
        else:
            roots.append(resp)
    return roots


@router.post("/departments", response_model=DepartmentResponse, status_code=201)
async def create_department(
    data: DepartmentCreate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """创建部门"""
    if data.parent_id:
        parent_result = await db.execute(
            select(Department).where(Department.id == data.parent_id)
        )
        if not parent_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="上级部门不存在")
    dept = Department(name=data.name, parent_id=data.parent_id)
    db.add(dept)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="已存在同名部门")
    await db.refresh(dept)
    await audit_service.record_audit(db, current_user.id, current_user.username, "department.create", resource_type="department", resource_id=dept.id,
        detail={"name": dept.name}, request=request,
    )
    return DepartmentResponse(
        id=dept.id, name=dept.name, parent_id=dept.parent_id,
        children=[], created_at=dept.created_at,
    )


@router.patch("/departments/{dept_id}", response_model=MessageResponse)
async def update_department(
    dept_id: str,
    data: DepartmentUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """修改部门名称/上级部门（防把自己或子孙设为上级）"""
    result = await db.execute(select(Department).where(Department.id == dept_id))
    dept = result.scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="部门不存在")

    if data.parent_id is not None:
        if data.parent_id == dept_id:
            raise HTTPException(status_code=400, detail="上级部门不能是自己")
        # 收集子孙 ID：新上级不能落在自己的子孙里（防成环）
        all_result = await db.execute(select(Department))
        id_to_parent = {d.id: d.parent_id for d in all_result.scalars().all()}
        descendants: set[str] = set()

        def _walk(did: str) -> None:
            for cid, pid in id_to_parent.items():
                if pid == did and cid not in descendants:
                    descendants.add(cid)
                    _walk(cid)

        _walk(dept_id)
        if data.parent_id in descendants:
            raise HTTPException(status_code=400, detail="上级部门不能是自己的下级部门")
        # 校验新上级存在（此前漏校验：不存在的 parent_id 会触发 FK IntegrityError，
        # 被误报成"已存在同名部门"，误导管理员）
        parent_result = await db.execute(
            select(Department).where(Department.id == data.parent_id)
        )
        if not parent_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="上级部门不存在")
        dept.parent_id = data.parent_id
    if data.name is not None:
        dept.name = data.name
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="已存在同名部门")
    await audit_service.record_audit(db, current_user.id, current_user.username, "department.update", resource_type="department", resource_id=dept_id,
        detail=data.model_dump(exclude_none=True), request=request,
    )
    return MessageResponse(message="部门已更新")


@router.delete("/departments/{dept_id}", response_model=MessageResponse)
async def delete_department(
    dept_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除部门（有子部门时拒绝；部门相关授权与成员关系一并清理）"""
    result = await db.execute(select(Department).where(Department.id == dept_id))
    dept = result.scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="部门不存在")

    child_result = await db.execute(
        select(func.count(Department.id)).where(Department.parent_id == dept_id)
    )
    if (child_result.scalar() or 0) > 0:
        raise HTTPException(status_code=400, detail="请先删除该部门的子部门")

    await db.execute(
        delete(KbMember).where(
            KbMember.subject_type == "dept", KbMember.subject_id == dept_id
        )
    )
    await db.execute(delete(UserDepartment).where(UserDepartment.department_id == dept_id))
    await db.execute(delete(Department).where(Department.id == dept_id))
    await db.flush()
    await audit_service.record_audit(db, current_user.id, current_user.username, "department.delete", resource_type="department", resource_id=dept_id,
        detail={"name": dept.name}, request=request,
    )
    return MessageResponse(message="部门已删除")


# ─────────────────────────── 用户-部门绑定 ───────────────────────────

@router.get("/users/{user_id}/departments", response_model=list[DepartmentResponse])
async def get_user_departments(
    user_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """用户所属部门列表"""
    result = await db.execute(
        select(Department)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .where(UserDepartment.user_id == user_id)
        .order_by(Department.created_at)
    )
    departments = result.scalars().all()
    return [
        DepartmentResponse(
            id=d.id, name=d.name, parent_id=d.parent_id,
            children=[], created_at=d.created_at,
        )
        for d in departments
    ]


@router.put("/users/{user_id}/departments", response_model=MessageResponse)
async def set_user_departments(
    user_id: str,
    data: UserDepartmentsUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """设置用户所属部门（整体替换；部门必须存在）"""
    result = await db.execute(select(User).where(User.id == user_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="用户不存在")

    # 去重（重复 ID 会撞唯一约束 uq_user_department 抛 500）
    dept_ids = list(dict.fromkeys(data.department_ids))
    if dept_ids:
        r = await db.execute(
            select(Department.id).where(Department.id.in_(dept_ids))
        )
        found = {row[0] for row in r.all()}
        missing = [d for d in set(dept_ids) if d not in found]
        if missing:
            raise HTTPException(status_code=400, detail=f"部门不存在: {', '.join(missing)}")

    await db.execute(delete(UserDepartment).where(UserDepartment.user_id == user_id))
    for dept_id in dept_ids:
        db.add(UserDepartment(user_id=user_id, department_id=dept_id))
    await db.flush()
    await audit_service.record_audit(db, current_user.id, current_user.username, "user.departments_set", resource_type="user", resource_id=user_id,
        detail={"department_ids": dept_ids}, request=request,
    )
    return MessageResponse(message="用户部门已更新")


# ── 外部接入凭证（批次D：长期 MCP 接入——"发钥匙的柜台"） ──

class McpCredentialCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    kb_ids: list[str] | None = None  # 可见库白名单；None=不限（按绑定账号权限兜底）


@router.get("/mcp-credentials")
async def list_mcp_credentials(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """接入凭证列表（凭证原文哈希存储，列表永不返回原文）"""
    result = await db.execute(
        select(McpCredential, User.username)
        .join(User, McpCredential.owner_user_id == User.id)
        .order_by(desc(McpCredential.created_at))
    )
    return {
        "items": [{
            "id": c.id,
            "name": c.name,
            "owner_username": username,
            "kb_ids": c.kb_ids,
            "revoked": c.revoked,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "revoked_at": c.revoked_at.isoformat() if c.revoked_at else None,
        } for c, username in result.all()],
    }


@router.post("/mcp-credentials")
async def create_mcp_credential(
    data: McpCredentialCreate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """新建接入凭证——原文只在本次响应展示一次，库里只存 sha256 哈希"""
    import hashlib
    import secrets

    # 校验库白名单中的库真实存在
    if data.kb_ids:
        kb_rows = (await db.execute(
            select(KnowledgeBase.id).where(KnowledgeBase.id.in_(data.kb_ids))
        )).scalars().all()
        if len(kb_rows) != len(set(data.kb_ids)):
            raise HTTPException(status_code=400, detail="存在无效的知识库 ID")

    token = "mcpk_" + secrets.token_hex(24)
    cred = McpCredential(
        name=data.name,
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        owner_user_id=current_user.id,
        kb_ids=data.kb_ids or None,
    )
    db.add(cred)
    await db.flush()
    await audit_service.record_audit(db, current_user.id, current_user.username, "mcp.credential_create",
        resource_type="mcp_credential", resource_id=cred.id,
        detail={"name": data.name, "kb_ids": data.kb_ids}, request=request,
    )
    await db.commit()
    return {
        "id": cred.id,
        "name": cred.name,
        "token": token,  # 一次性展示；此后不可再获取
        "kb_ids": cred.kb_ids,
        "message": "凭证原文仅本次展示，请立即复制保存",
    }


@router.post("/mcp-credentials/{credential_id}/revoke", response_model=MessageResponse)
async def revoke_mcp_credential(
    credential_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """撤销接入凭证——立即失效（与顶号无关的独立吊销开关）"""
    cred = (await db.execute(
        select(McpCredential).where(McpCredential.id == credential_id)
    )).scalar_one_or_none()
    if cred is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    if cred.revoked:
        raise HTTPException(status_code=400, detail="凭证已撤销")
    cred.revoked = True
    cred.revoked_at = tz_now()
    await audit_service.record_audit(db, current_user.id, current_user.username, "mcp.credential_revoke",
        resource_type="mcp_credential", resource_id=cred.id,
        detail={"name": cred.name}, request=request,
    )
    await db.commit()
    return MessageResponse(message=f"已撤销接入凭证「{cred.name}」")


# ── 检索评估（产品化 B4：试检索 + 评估集跑分，管理端可视化） ──

class RetrievalTestRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(6, ge=1, le=20)
    with_rerank: bool = False


class EvalRunRequest(BaseModel):
    with_rerank: bool = False
    eval_set: str = "all"  # 验收批次1：all/regression/long（分层评估）


class RetrievalTermsRequest(BaseModel):
    """检索提准配置（验收批次1·1a）：同义词表 + 词权重，整体替换式保存"""
    synonyms: dict[str, list[str]] = {}
    weights: dict[str, float] = {}


class KbRetrievalConfigRequest(BaseModel):
    """库级检索参数（验收批次1·1d）：None=全局默认，与全局默认解耦"""
    top_k: int | None = None
    pagerank_alpha: float | None = None


@router.get("/knowledge-bases/{kb_id}/retrieval-config")
async def get_kb_retrieval_config(
    kb_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """读取库级检索配置（空对象=全用全局默认）"""
    kb = (await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
    )).scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb.retrieval_config or {}


@router.put("/knowledge-bases/{kb_id}/retrieval-config")
async def save_kb_retrieval_config(
    kb_id: str,
    data: KbRetrievalConfigRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """保存库级检索配置（只更新传入字段；null 值=清除该项回全局默认）"""
    kb = (await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
    )).scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if data.top_k is not None and not (1 <= data.top_k <= 50):
        raise HTTPException(status_code=400, detail="top_k 须在 1~50")
    if data.pagerank_alpha is not None and not (0 <= data.pagerank_alpha <= 1):
        raise HTTPException(status_code=400, detail="pagerank_alpha 须在 0~1")

    cfg = dict(kb.retrieval_config or {})
    for field in ("top_k", "pagerank_alpha"):
        value = getattr(data, field)
        if value is None:
            cfg.pop(field, None)  # null=清除，回全局默认
        else:
            cfg[field] = value
    kb.retrieval_config = cfg or None
    await audit_service.record_audit(
        db, current_user.id, current_user.username, "kb.retrieval_config_update",
        resource_type="knowledge_base",
        detail={"kb_id": kb_id, "config": cfg},
    )
    await db.commit()
    return {"message": "库级检索配置已保存", "config": cfg}


@router.post("/retrieval-terms/suggest")
async def suggest_retrieval_terms(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """同义词自动发现（验收批次1·8g）：扫最近会话的用户问法，让 LLM 找出
    "用户问法用词 vs 知识库文档用词"的错配——手动触发一次扫描，建议进入
    待审列表（后台定时扫描与手动扫描共用同一套暂存）"""
    from app.services import retrieval_suggest

    suggestions = await retrieval_suggest.generate_suggestions(db)
    added = retrieval_suggest.store_suggestions(suggestions)
    if not suggestions:
        return {"suggestions": [], "message": "暂无会话数据可供分析"}
    if added == 0:
        return {"suggestions": [], "message": "扫描完成：无新建议（已有建议均在待审列表中）"}
    return {"suggestions": suggestions, "message": f"新增 {added} 条建议待审阅"}


@router.get("/retrieval-terms/suggestions")
async def get_retrieval_suggestions(current_user: User = Depends(require_admin)):
    """待审建议列表（自动扫描与手动扫描的结果，未采纳未忽略的）"""
    from app.services import retrieval_suggest

    return {"suggestions": retrieval_suggest.pending_suggestions()}


class SuggestionRequest(BaseModel):
    query_term: str
    doc_term: str


@router.post("/retrieval-terms/adopt")
async def adopt_retrieval_term(
    data: SuggestionRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """采纳建议：并入同义词配置并**立即保存生效**（8g 自动化：不再需要手动点保存）"""
    from app.rag import retrieval_terms
    from app.services import retrieval_suggest

    query_term = data.query_term.strip()
    doc_term = data.doc_term.strip()
    if not query_term or not doc_term:
        raise HTTPException(status_code=400, detail="query_term/doc_term 不能为空")
    current = retrieval_terms._load()
    synonyms = {k: list(v) for k, v in current["synonyms"].items()}
    existing = synonyms.get(query_term, [])
    if doc_term not in existing:
        synonyms[query_term] = existing + [doc_term]
    retrieval_terms.save_terms({"synonyms": synonyms, "weights": current["weights"]})
    try:
        retrieval_suggest.mark_suggestion(query_term, doc_term, seen=True)
    except Exception as e:
        # 质检低危修复：标记写盘失败不应 500（配置已生效），日志兜底
        logger.warning(f"建议标记失败（配置已生效，仅标记缺失）: {e}")
    try:
        await audit_service.record_audit(
            db, current_user.id, current_user.username, "retrieval.terms_adopt",
            resource_type="retrieval_config",
            detail={"query_term": query_term, "doc_term": doc_term},
        )
        await db.commit()
    except Exception as e:
        logger.error(f"采纳审计写入失败（配置已生效）: {e}")
        await db.rollback()
    return {"message": f"已采纳并生效：{query_term} → {doc_term}"}


@router.post("/retrieval-terms/ignore-suggestion")
async def ignore_retrieval_term(
    data: SuggestionRequest,
    current_user: User = Depends(require_admin),
):
    """忽略建议：标记已读（防重复弹出，可重新扫描生成）"""
    from app.services import retrieval_suggest

    retrieval_suggest.mark_suggestion(data.query_term.strip(), data.doc_term.strip(), seen=True)
    return {"message": "已忽略"}


@router.get("/retrieval-terms")
async def get_retrieval_terms(current_user: User = Depends(require_admin)):
    """读取检索提准配置（同义词表 + 词权重）"""
    from app.rag import retrieval_terms

    return retrieval_terms._load()


@router.put("/retrieval-terms")
async def save_retrieval_terms(
    data: RetrievalTermsRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """保存检索提准配置（整体替换；BM25 查询侧带 mtime 缓存，写入即时生效）"""
    from app.rag import retrieval_terms

    # 体量上限（质检低危修复）：防误配/恶意请求写入超大体量配置拖慢每次检索
    if len(data.synonyms) > 200 or len(data.weights) > 200:
        raise HTTPException(status_code=400, detail="配置条数超过上限（200）")
    for term, syns in data.synonyms.items():
        term = term.strip()
        if not term:
            raise HTTPException(status_code=400, detail="同义词的查询词不能为空")
        if len(syns) > 20:
            raise HTTPException(status_code=400, detail="单条同义词变体超过上限（20）")
        cleaned = []
        for s in syns:
            s = s.strip()
            if not s:
                raise HTTPException(status_code=400, detail="同义词变体不能为空")
            if s not in cleaned:
                cleaned.append(s)
        data.synonyms[term] = cleaned
    for term, weight in data.weights.items():
        if not term.strip():
            raise HTTPException(status_code=400, detail="加权词不能为空")
        if weight <= 0 or weight > 10:
            # 8a：打分侧逐词加权后支持 <1 降权（如 0.5 给通用词降噪）
            raise HTTPException(status_code=400, detail="词权重须在 (0, 10] 区间")

    retrieval_terms.save_terms({"synonyms": data.synonyms, "weights": data.weights})
    try:
        await audit_service.record_audit(
            db, current_user.id, current_user.username, "retrieval.terms_update",
            resource_type="retrieval_config",
            detail={"synonyms": len(data.synonyms), "weights": len(data.weights)},
        )
        await db.commit()
    except Exception as e:
        # 8d（质检 R1）：配置文件与审计库无跨介质事务——审计失败时配置已生效，
        # 至少留 error 日志兜底可追溯，不让"配置变了无任何痕迹"
        logger.error(f"检索配置审计写入失败（配置已生效但审计缺失）: {e}")
        await db.rollback()
    return {"message": "检索提准配置已保存，即时生效"}


@router.post("/eval/retrieval-test")
async def eval_retrieval_test(
    data: RetrievalTestRequest,
    current_user: User = Depends(require_admin),
):
    """试检索：输入问题即时查看混合检索命中切片（含 rerank 开关对比效果）。

    验收修复（A2）：doc_ids 在请求事件循环内 await 查询，to_thread 只包
    纯阻塞检索——此前整函数进线程再 asyncio.run 开新循环，连接跨循环 500。
    """
    from app.services import eval_service

    return await eval_service.retrieval_test_async(data.question, data.top_k, data.with_rerank)


@router.post("/eval/run")
async def eval_run(
    data: EvalRunRequest,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """评估集跑分：全量标准题+模糊题统计 hit-rate@6 / MRR@6 / 平均延迟。

    约需 15 秒（每题一次混合检索）；前端等待期间展示加载态。留审计（烧嵌入成本）。
    验收修复（A2）：与试检索同源修复，async 版直接在请求循环内执行。
    """
    from app.services import eval_service

    result = await eval_service.run_eval_async(data.with_rerank, data.eval_set)
    await audit_service.record_audit(
        db, current_user.id, current_user.username, "eval.run",
        resource_type="eval",
        detail={"with_rerank": data.with_rerank,
                "eval_set": data.eval_set,
                "questions": result.get("total_questions"),
                "hit_rate": result.get("total", {}).get("hit_rate")},
        request=request,
    )
    await db.commit()
    return result


@router.get("/approval-rules")
async def approval_rules_list(
    approver: str | None = None,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """审批规则列表（批次2-5 规则管理页）：业务/区间/审批人/动作 + 来源文档。
    管理员可视化校验规则解析产物（宁缺勿滥原则下解析不了的不在此列）。

    挂清单 #6 修复：approver 模糊搜索参数（规则多时人工翻页找审批人
    不便——验收 4.5 预期功能补实现）。"""
    from sqlalchemy import asc, select

    from app.models.rule import ApprovalRule

    query = select(ApprovalRule)
    if approver:
        # 质检 P3-3：转义 LIKE 通配符（%/_ 按字面搜索而非通配）
        escaped = approver.replace("\\", "\\\\").replace("%", "\%").replace("_", "\_")
        query = query.where(ApprovalRule.approver.contains(escaped))
    query = query.order_by(
        asc(ApprovalRule.business_type), asc(ApprovalRule.min_value)
    )
    rows = (await db.execute(query)).scalars().all()

    doc_names: dict[str, str] = {}
    if rows:
        from app.models.document import Document

        doc_ids = {r.source_doc_id for r in rows}
        doc_rows = (await db.execute(
            select(Document.id, Document.original_name).where(
                Document.id.in_(doc_ids)
            )
        )).all()
        doc_names = {d[0]: d[1] for d in doc_rows}

    return [
        {
            "id": r.id,
            "business_type": r.business_type,
            "min_value": r.min_value,
            "max_value": r.max_value,
            "unit": r.unit,
            "approver": r.approver,
            "action": r.action,
            "source_text": r.source_text,
            "source_doc": doc_names.get(r.source_doc_id, "已删除文档"),
        }
        for r in rows
    ]


@router.get("/approval-rules/conflicts")
async def approval_rules_conflicts(
    current_user: User = Depends(require_admin),
):
    """规则冲突列表（批次3/3.5）：同业务区间重叠（完全相等/部分重叠/门槛平移）不同审批人的规则对——
    旧版与新版制度并存场景（旧版存档），附裁决方向（制度生效日期优先，无则回退上传时间）。"""
    from app.rag.rules import service as rules_service

    return await rules_service.find_conflicts()


@router.get("/documents/{doc_id}/versions")
async def document_versions(
    doc_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """文档版本链（批次5 7b）：旧版在前、新版在后，含状态与版本号"""
    from app.services import document_service

    return await document_service.get_version_chain(db, doc_id)


@router.get("/documents/{doc_id}/content")
async def document_content(
    doc_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """指定版本（或当前版）的纯文本内容——版本 diff 视图的数据源
    （批次F 前置·diff 视图：版本链只有元数据，diff 需要内容）。
    文本提取复用 loader（txt/md/pdf/docx/csv）；xlsx 转行文本；
    图片文档无文本可提，返回空串+说明。文件缺失时 400（提示重新上传）。"""
    target = (await db.execute(select(Document).where(Document.id == doc_id))
              ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    path = target.storage_path
    if not path or not os.path.exists(path):
        raise HTTPException(
            status_code=400, detail="文档物理文件已不存在（请重新上传）"
        )
    ext = Path(path).suffix.lower()
    try:
        if ext in (".txt", ".md", ".pdf", ".docx", ".csv"):
            # 质检 P3-4：同步文件解析走 to_thread（大 PDF 不阻塞事件循环）
            from app.pipeline.loader import load_document

            content = await asyncio.to_thread(load_document, path)
        elif ext == ".xlsx":
            from app.pipeline.loader import load_table

            sheets = await asyncio.to_thread(
                load_table, path, target.original_name
            )
            lines = []
            for s in sheets:
                lines.append(f"# {s.get('name', '')}")
                for row in s.get("rows", []):
                    # 质检 P3-8：空单元格显示为空白而非 "None"
                    lines.append(
                        " | ".join("" if c is None else str(c) for c in row)
                    )
            content = "\n".join(lines)
        else:
            # 图片/未知格式：无纯文本（picture 文档的入库文本是合成描述，
            # 不作为 diff 对比对象——前端对此显示说明）
            content = ""
    except Exception as e:
        # 质检 P3-3：原始异常只进服务端日志（pypdf 等内部信息不外泄给
        # 管理端），前端拿通用中文提示
        logger.exception("版本内容提取失败（%s）: %s", target.original_name, e)
        raise HTTPException(
            status_code=422,
            detail=f"内容提取失败（{target.original_name}），请检查文件完整性",
        )
    return {
        "doc_id": doc_id,
        "version_no": target.version_no,
        "filename": target.original_name,
        "content_type": target.content_type,
        "content": content,
    }


@router.post("/documents/{doc_id}/restore")
async def document_restore(
    doc_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """版本回溯（批次5 7b）：旧版恢复为当前版本、当前版标 superseded。
    留审计（制度回滚是敏感操作，审计日志与版本链构成完整证据链）。"""
    from app.services import document_service

    result = await document_service.restore_version(db, doc_id)
    await audit_service.record_audit(
        db, current_user.id, current_user.username, "document.restore",
        resource_type="document", resource_id=doc_id,
        detail={"restored_version_no": result["version_no"]},
        request=request,
    )
    await db.commit()
    return result


# ── API key 管理（批次11e OpenAI 兼容 API） ──

def _parse_expires_at(raw: str) -> datetime | None:
    """expires_at 字符串 → aware datetime；空串/None=永不过期。
    批次B 质检 P3：创建/修改两处解析逻辑抽 helper，并拒绝过去时间
    （已过期的 key 没有设置意义，防误操作）。"""
    from datetime import datetime, timezone

    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="expires_at 格式无效（需 ISO 8601）"
        )
    if expires < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400, detail="expires_at 已过期，请设置未来时间"
        )
    return expires


@router.post("/api-keys", status_code=201)
async def api_key_create(
    data: dict,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """生成 API key（明文只返回一次；SHA-256 哈希入库）。留审计。

    批次A·11e backlog：expires_at 管理入口——创建时可选传 expires_at
    （ISO 8601 字符串，如 "2027-01-01T00:00:00Z"）设置过期时间；
    不传=永不过期。批次B P3：过去时间 400（见 _parse_expires_at）。
    批次F 挂清单 #12：kb_ids 库白名单（可选，null=穿透账号权限）——
    校验库存在性，非白名单库问题由鉴权侧收敛（见 openai_compat）。"""
    import hashlib
    import secrets
    import uuid as uuid_mod

    from app.models.api_key import ApiKey

    expires_at = _parse_expires_at(data.get("expires_at"))
    kb_ids = data.get("kb_ids")
    if kb_ids is not None:
        if not isinstance(kb_ids, list) or any(not isinstance(k, str) or not k for k in kb_ids):
            raise HTTPException(status_code=400, detail="kb_ids 必须是知识库 ID 字符串数组")
        kb_ids = list(dict.fromkeys(kb_ids))  # 去重保序
        if kb_ids:
            existing = (await db.execute(
                select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids))
            )).scalars().all()
            missing = set(kb_ids) - set(existing)
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"知识库不存在：{', '.join(sorted(missing))}",
                )
    plain = f"sk-rag-{secrets.token_hex(24)}"
    key = ApiKey(
        id=str(uuid_mod.uuid4()),
        key_hash=hashlib.sha256(plain.encode()).hexdigest(),
        key_prefix=plain[:16],
        name=str(data.get("name", "未命名"))[:100],
        kb_ids=kb_ids or None,
        user_id=current_user.id,
        expires_at=expires_at,
    )
    db.add(key)
    await db.commit()
    await audit_service.record_audit(
        db, current_user.id, current_user.username, "api_key.create",
        resource_type="api_key", resource_id=key.id,
        detail={"name": key.name,
                "kb_ids": kb_ids or None,
                "expires_at": expires_at.isoformat() if expires_at else None},
        request=request,
    )
    await db.commit()
    return {"id": key.id, "key": plain, "name": key.name,
            "prefix": key.key_prefix, "created_at": key.created_at.isoformat(),
            "kb_ids": kb_ids or None,
            "expires_at": expires_at.isoformat() if expires_at else None}


@router.get("/api-keys")
async def api_key_list(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """API key 列表（不含明文——明文只生成时返回一次）"""
    from app.models.api_key import ApiKey

    rows = (await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars().all()
    return [
        {"id": k.id, "name": k.name, "prefix": k.key_prefix,
         "is_active": k.is_active,
         "kb_ids": k.kb_ids,
         "expires_at": k.expires_at.isoformat() if k.expires_at else None,
         "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
         "created_at": k.created_at.isoformat()}
        for k in rows
    ]


@router.patch("/api-keys/{key_id}/revoke")
async def api_key_revoke(
    key_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """吊销 API key。留审计。"""
    from app.models.api_key import ApiKey

    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="API key 不存在")
    key.is_active = False
    await db.commit()
    await audit_service.record_audit(
        db, current_user.id, current_user.username, "api_key.revoke",
        resource_type="api_key", resource_id=key_id,
        detail={"name": key.name}, request=request,
    )
    await db.commit()
    return {"message": "已吊销"}


@router.patch("/api-keys/{key_id}/expiry")
async def api_key_set_expiry(
    key_id: str,
    data: dict,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """设置/清除 API key 过期时间（批次A·11e backlog 管理入口）。留审计。

    data.expires_at：ISO 8601 字符串设置过期；null/空串=清除（永不过期）。
    鉴权侧（openai_compat._auth_by_api_key）已按 expires_at 拒过期 key。
    批次B P3：解析抽 _parse_expires_at（含过去时间 400）。"""
    from app.models.api_key import ApiKey

    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="API key 不存在")
    expires = _parse_expires_at(data.get("expires_at"))
    key.expires_at = expires
    await db.commit()
    await audit_service.record_audit(
        db, current_user.id, current_user.username, "api_key.expiry_update",
        resource_type="api_key", resource_id=key_id,
        detail={"name": key.name,
                "expires_at": expires.isoformat() if expires else None},
        request=request,
    )
    await db.commit()
    return {"message": "已更新过期时间",
            "expires_at": expires.isoformat() if expires else None}


# ── MCP Server 工具管理（批次F 挂清单 #13 页面化）──
# 配置入库（此前仅 .env 手改+重启）；问答链路经 tools.refresh_mcp_config
# 读库。删除/停用/更新后使配置与工具元数据缓存失效（用完即弃模式
# 下无常驻会话需回收，_drop_stdio_session 为空操作兼容保留）。

def _mcp_server_dict(s) -> dict:
    import json

    # 质检 P3-3：DB 数据被手改损坏时降级空参数（不 500）
    try:
        args = json.loads(s.args_json) if s.args_json else []
    except json.JSONDecodeError:
        args = []
    return {
        "id": s.id, "name": s.name, "transport": s.transport,
        "url": s.url, "command": s.command,
        "args": args,
        "enabled": s.enabled,
        "created_at": s.created_at.isoformat(),
    }


@router.get("/mcp-servers")
async def mcp_server_list(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """外部 MCP Server 配置列表（页面化 #13）"""
    from app.models.mcp_server import McpServer

    rows = (await db.execute(
        select(McpServer).order_by(McpServer.created_at)
    )).scalars().all()
    return [_mcp_server_dict(s) for s in rows]


@router.post("/mcp-servers", status_code=201)
async def mcp_server_create(
    data: dict,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """新增外部 MCP Server 配置。留审计。

    http：{"name", "transport": "http", "url"}；
    stdio：{"name", "transport": "stdio", "command", "args": [...]}"""
    import json
    import uuid as uuid_mod

    from app.models.mcp_server import McpServer
    from app.rag import tools as tools_mod

    name = str(data.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="服务名不能为空")
    transport = str(data.get("transport", "")).strip()
    if transport not in ("http", "stdio"):
        raise HTTPException(status_code=400, detail="transport 必须是 http 或 stdio")
    dup = (await db.execute(
        select(McpServer).where(McpServer.name == name)
    )).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=400, detail=f"服务名 {name} 已存在")
    url = command = None
    args_json = None
    if transport == "http":
        url = str(data.get("url", "")).strip()
        if not url:
            raise HTTPException(status_code=400, detail="http 传输必须提供 url")
    else:
        command = str(data.get("command", "")).strip()
        if not command:
            raise HTTPException(status_code=400, detail="stdio 传输必须提供 command")
        args = data.get("args") or []
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            raise HTTPException(status_code=400, detail="args 必须是字符串数组")
        args_json = json.dumps(args)
    # 质检 P3-4：字符串 "false" 会被 bool() 判 True——显式解析
    enabled_raw = data.get("enabled", True)
    enabled = enabled_raw if not isinstance(enabled_raw, str) \
        else enabled_raw.lower() not in ("false", "0", "")
    row = McpServer(
        id=str(uuid_mod.uuid4()),
        name=name, transport=transport,
        url=url, command=command, args_json=args_json,
        enabled=enabled,
    )
    db.add(row)
    await db.commit()
    tools_mod.invalidate_mcp_config()
    await audit_service.record_audit(
        db, current_user.id, current_user.username, "mcp_server.create",
        resource_type="mcp_server", resource_id=row.id,
        detail={"name": name, "transport": transport}, request=request,
    )
    await db.commit()
    return _mcp_server_dict(row)


@router.delete("/mcp-servers/{server_id}")
async def mcp_server_delete(
    server_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除外部 MCP Server 配置（同时回收其 stdio 常驻进程）。留审计。"""
    from app.models.mcp_server import McpServer
    from app.rag import tools as tools_mod

    row = (await db.execute(
        select(McpServer).where(McpServer.id == server_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP Server 配置不存在")
    name = row.name
    await db.delete(row)
    await db.commit()
    tools_mod.invalidate_mcp_config()
    await tools_mod._drop_stdio_session(name)  # 进程回收
    await audit_service.record_audit(
        db, current_user.id, current_user.username, "mcp_server.delete",
        resource_type="mcp_server", resource_id=server_id,
        detail={"name": name}, request=request,
    )
    await db.commit()
    return {"message": "已删除"}


@router.patch("/mcp-servers/{server_id}/toggle")
async def mcp_server_toggle(
    server_id: str,
    data: dict,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """启停外部 MCP Server（停用时使配置/工具元数据缓存失效）。留审计。"""
    from app.models.mcp_server import McpServer
    from app.rag import tools as tools_mod

    row = (await db.execute(
        select(McpServer).where(McpServer.id == server_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP Server 配置不存在")
    # 质检 P3-4：同 create 的显式解析
    enabled_raw = data.get("enabled", True)
    row.enabled = enabled_raw if not isinstance(enabled_raw, str) \
        else enabled_raw.lower() not in ("false", "0", "")
    await db.commit()
    tools_mod.invalidate_mcp_config()
    if not row.enabled:
        await tools_mod._drop_stdio_session(row.name)
    await audit_service.record_audit(
        db, current_user.id, current_user.username, "mcp_server.toggle",
        resource_type="mcp_server", resource_id=server_id,
        detail={"name": row.name, "enabled": row.enabled}, request=request,
    )
    await db.commit()
    return _mcp_server_dict(row)


@router.post("/mcp-servers/test")
async def mcp_server_test(
    data: dict,
    current_user: User = Depends(require_admin),
):
    """测试连接：按提交的配置试连 MCP Server 并列出工具（不落库）。

    body 与创建接口同构（http: url；stdio: command+args）。
    成功返回 {"ok": true, "tools": [{"name", "description"}...]}；
    失败返回 {"ok": false, "error": 可读错误}（不抛 500，页面直接展示）。"""
    import httpx
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.session import ClientSession

    from app.rag import tools as tools_mod

    import uuid as uuid_mod

    transport = str(data.get("transport", "")).strip()
    # 质检 P2-4：一次性唯一会话键——复用传入 name 会命中线上常驻会话
    # （结果失真）且随后的 drop 误杀线上会话
    test_key = f"__test__{uuid_mod.uuid4().hex[:8]}"
    try:
        if transport == "stdio":
            command = str(data.get("command", "")).strip()
            if not command:
                return {"ok": False, "error": "stdio 传输必须提供 command"}
            args = data.get("args") or []
            got = await tools_mod._get_stdio_session(
                test_key, {"command": command, "args": [str(a) for a in args]}
            )
            if got is None:
                return {"ok": False, "error": "stdio 进程启动失败（命令不可用或超时）"}
            streams, session = got
            try:
                result = await session.list_tools()
            finally:
                await tools_mod._close_stdio_session(streams, session, test_key)
        elif transport == "http":
            url = str(data.get("url", "")).strip()
            if not url:
                return {"ok": False, "error": "http 传输必须提供 url"}
            # 质检 P2-5：scheme 白名单 http/https（防 file:// 等本地协议；
            # 接口本身 admin-only，且错误仅回类型名不回包体——盲 SSRF 低风险）
            if not url.lower().startswith(("http://", "https://")):
                return {"ok": False, "error": "url 仅支持 http/https 协议"}
            async with httpx.AsyncClient(timeout=15) as http:
                async with streamable_http_client(url, http_client=http) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        result = await session.list_tools()
        else:
            return {"ok": False, "error": "transport 必须是 http 或 stdio"}
        tools_out = [
            {"name": t.name, "description": t.description or ""}
            for t in result.tools
        ]
        return {"ok": True, "tools": tools_out}
    except Exception as e:
        logger.warning("MCP 测试连接失败: {} - {}", test_key, e)
        return {"ok": False, "error": f"连接失败：{type(e).__name__}"}


# ── RAPTOR 摘要树可视化（批次B）──

@router.get("/raptor/overview")
async def raptor_overview(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """摘要树概览：每个有树的文档 → 层级与簇结构（可视化页数据源）。
    摘要块文本从 Chroma 拉取（块数少，全量可接受）。"""
    from app.models.document import Document
    from app.models.raptor_cluster import RaptorCluster

    rows = (await db.execute(
        select(RaptorCluster, Document.original_name)
        .join(Document, Document.id == RaptorCluster.document_id)
        .order_by(RaptorCluster.tree_level, RaptorCluster.cluster_id)
    )).all()
    if not rows:
        return {"documents": []}

    # Chroma 摘要块文本（document_id → {chunk_index: 文本}）。
    # 批次B.5 P3-6：逐文档 get 加 try/except——与 tree_traversal_search
    # 的逐文档保护同口径（单文档异常不拖垮整个概览端点）
    from app.rag.retriever import get_vector_store

    def _fetch_summaries(doc_ids: list[str]) -> dict[str, dict]:
        collection = get_vector_store()._collection
        out: dict[str, dict] = {}
        for doc_id in doc_ids:
            try:
                r = collection.get(
                    where={"document_id": doc_id},
                    include=["documents", "metadatas"],
                )
            except Exception as e:
                logger.debug(f"摘要树概览取块失败（{doc_id}，跳过）: {e}")
                out[doc_id] = {}
                continue
            texts = {}
            for content, meta in zip(r.get("documents") or [],
                                     r.get("metadatas") or [{}]):
                if meta.get("chunk_type") == "raptor_summary":
                    texts[meta.get("chunk_index", -1)] = content or ""
            out[doc_id] = texts
        return out

    doc_ids = list({r[0].document_id for r in rows})
    summaries = await asyncio.to_thread(_fetch_summaries, doc_ids)

    by_doc: dict[str, dict] = {}
    for cluster, doc_name in rows:
        entry = by_doc.setdefault(cluster.document_id, {
            "doc_id": cluster.document_id,
            "filename": doc_name,
            "clusters": [],
        })
        text = summaries.get(cluster.document_id, {}).get(
            cluster.summary_chunk_index, ""
        )
        entry["clusters"].append({
            "tree_level": cluster.tree_level,
            "cluster_id": cluster.cluster_id,
            "child_count": len(cluster.child_chunk_indices or []),
            "child_chunk_indices": list(cluster.child_chunk_indices or []),
            "summary_preview": text[:200],
            "built_at": cluster.built_at.isoformat() if cluster.built_at else None,
        })
    return {"documents": list(by_doc.values())}


# ── 知识图谱可视化与社区检测（批次2-3） ──

@router.get("/graph/overview")
async def graph_overview(
    current_user: User = Depends(require_admin),
):
    """图谱概览：实体节点/关系边/社区报告（可视化页数据源）。
    图谱关闭/不可用时返回空结构（前端展示空态引导）。"""
    from app.rag.graph import community as community_service

    return await community_service.graph_overview()


@router.post("/graph/communities/refresh")
async def graph_communities_refresh(
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """手动触发社区检测（Leiden 分区 + LLM 社区报告）。留审计。
    建图补建完成后也会自动触发；此端点用于图已就绪时管理员按需重算。"""
    from app.rag.graph import community as community_service

    result = await community_service.run_community_detection()
    await audit_service.record_audit(
        db, current_user.id, current_user.username, "graph.communities_refresh",
        resource_type="graph",
        detail={"community_count": result.get("community_count")},
        request=request,
    )
    await db.commit()
    return result
