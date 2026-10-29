"""知识库权限服务（企业知识库改造 阶段1）

权限规则（一句话）：个人授权 > 部门授权 > 默认无权限；admin 恒有全部权限。
can_manage 隐含 can_upload 与 can_read；授予 upload/manage 时 can_read 强制补真。

安全要点：所有"用户可见库"计算集中在本模块，检索链路必须 fail-closed——
任何检索入口不传可见文档范围就报错，防权限过滤被漏掉导致越权读到别的部门文档。
"""
from fastapi import HTTPException
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.knowledge_base import KbMember, KnowledgeBase, UserDepartment
from app.models.user import User

ALL_PERMS = {"can_read": True, "can_upload": True, "can_manage": True}


async def get_user_department_ids(db: AsyncSession, user_id: str) -> list[str]:
    """用户所属部门 ID 列表"""
    result = await db.execute(
        select(UserDepartment.department_id).where(UserDepartment.user_id == user_id)
    )
    return [row[0] for row in result.all()]


async def get_user_kb_perms(db: AsyncSession, user: User) -> dict[str, dict]:
    """用户对每个知识库的权限映射 {kb_id: {can_read, can_upload, can_manage}}。

    admin 直接返回全库全权限（不查表）。
    editor（企业改造阶段3）：全局内容维护身份——对所有库 can_upload=True
    （可见性仍受 read 授权约束，管理权仍按授权）。
    普通用户 = 公共库（只读）+ 授权命中库；授权取个人行与部门行中的最高权限。
    """
    kb_result = await db.execute(select(KnowledgeBase))
    all_kbs = kb_result.scalars().all()

    if user.role == "admin":
        return {kb.id: dict(ALL_PERMS) for kb in all_kbs}

    editor = user.role == "editor"
    perms: dict[str, dict] = {
        kb.id: {
            "can_read": True,
            "can_upload": False,
            "can_manage": False,
        }
        for kb in all_kbs
        if kb.is_public
    }

    dept_ids = await get_user_department_ids(db, user.id)
    # 注意：个人授权必须是「type=user 且 id=本人」两个条件相与——
    # 曾写成两个独立条件交给 or_，`subject_type == "user"` 单独成立，
    # 会匹配全系统所有人的个人授权行（任何用户继承他人授权 = 越权）。
    grant_conds = [
        (KbMember.subject_type == "user") & (KbMember.subject_id == user.id)
    ]
    if dept_ids:
        grant_conds.append(
            (KbMember.subject_type == "dept") & (KbMember.subject_id.in_(dept_ids))
        )
    result = await db.execute(select(KbMember).where(or_(*grant_conds)))
    for grant in result.scalars().all():
        cur = perms.setdefault(
            grant.kb_id,
            {"can_read": False, "can_upload": False, "can_manage": False},
        )
        cur["can_read"] = cur["can_read"] or grant.can_read
        cur["can_upload"] = cur["can_upload"] or grant.can_upload
        cur["can_manage"] = cur["can_manage"] or grant.can_manage

    # 规范化：能传/能管必然能读；editor 在可见库内必然可传（内容维护身份，
    # 但看不见的库不可读也不可传——不进未授权私有库的浏览列表）
    for p in perms.values():
        if p["can_upload"] or p["can_manage"]:
            p["can_read"] = True
        if editor and p["can_read"]:
            p["can_upload"] = True
    return perms


async def get_visible_kb_ids(db: AsyncSession, user: User) -> list[str]:
    """用户可读（问答检索可见）的知识库 ID 列表。admin 恒为全部。"""
    return [
        kb_id for kb_id, p in (await get_user_kb_perms(db, user)).items()
        if p["can_read"]
    ]


async def get_visible_doc_ids(db: AsyncSession, user: User) -> list[str]:
    """用户可读的全部文档 ID（检索链路 fail-closed 的过滤依据）。
    批次5 7b：superseded（被新版替换）的旧版本不参与检索——检索只走
    当前有效版本，旧版数据保留在版本历史中可回溯"""
    kb_ids = await get_visible_kb_ids(db, user)
    if not kb_ids:
        return []
    result = await db.execute(
        select(Document.id).where(
            Document.kb_id.in_(kb_ids),
            Document.status != "superseded",
        )
    )
    return [row[0] for row in result.all()]


async def get_kb_perm(db: AsyncSession, user: User, kb_id: str) -> dict:
    """用户对指定知识库的权限（admin 恒全权限；不存在返回全 False）"""
    if user.role == "admin":
        return dict(ALL_PERMS)
    perms = await get_user_kb_perms(db, user)
    return perms.get(kb_id, {"can_read": False, "can_upload": False, "can_manage": False})


async def require_kb_perm(
    db: AsyncSession, user: User, kb_id: str, perm: str
) -> None:
    """校验用户对知识库的某项权限，不满足抛 403（越权信息统一话术，不泄露库是否存在）"""
    p = await get_kb_perm(db, user, kb_id)
    if not p.get(perm):
        raise HTTPException(
            status_code=403,
            detail="没有该知识库的操作权限，请联系管理员",
        )
