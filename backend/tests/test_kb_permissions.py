"""知识库/部门/权限矩阵测试（企业知识库改造 阶段1）

权限规则验证：个人授权 > 部门授权 > 默认无权限；admin 恒有全部权限；
can_manage 隐含 can_upload/can_read；公共库全员可读。
"""
import json

import pytest
from httpx import AsyncClient

from app.infrastructure.database import async_session_factory


async def _db_user_id(username: str) -> str:
    """按用户名取用户 ID"""
    from sqlalchemy import select
    from app.models.user import User

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.username == username))
        return result.scalar_one().id


async def _register_login(client: AsyncClient, username: str) -> str:
    """建号并登录一个新用户（阶段3：注册接口已关闭，直接落库），返回 access_token"""
    from tests.conftest import create_user_in_db
    await create_user_in_db(username, f"{username}@test.com", "123456")
    resp = await client.post("/api/v1/auth/login", json={
        "username": username, "password": "123456",
    })
    return resp.json()["access_token"]


async def _seed_doc(kb_id: str, name: str, uploader_id: str) -> str:
    """直接落库一个已完成文档（不触发向量处理），返回文档 ID"""
    from app.models.document import Document

    async with async_session_factory() as db:
        doc = Document(
            filename=name, original_name=name, file_type="txt",
            file_size=10, storage_path=f"/tmp/{name}", kb_id=kb_id,
            content_type="long_text", status="completed", chunk_count=1,
            uploaded_by=uploader_id,
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        return doc.id


async def _grant_kb(admin_client: AsyncClient, kb_id: str, members: list[dict]) -> None:
    """整体替换库授权"""
    resp = await admin_client.put(
        f"/api/v1/admin/knowledge-bases/{kb_id}/members", json={"members": members}
    )
    assert resp.status_code == 200, resp.text


# ════════════════════════════════════════════════════
# 知识库 CRUD（admin）
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_admin_kb_crud(admin_client: AsyncClient):
    """创建/列表/更新/删除知识库；重名拒绝；有文档时删除拒绝"""
    # 创建
    resp = await admin_client.post("/api/v1/admin/knowledge-bases", json={
        "name": "人事制度库", "description": "HR 制度", "is_public": False,
    })
    assert resp.status_code == 201
    kb_id = resp.json()["id"]
    assert resp.json()["is_public"] is False

    # 重名拒绝
    resp = await admin_client.post("/api/v1/admin/knowledge-bases", json={
        "name": "人事制度库",
    })
    assert resp.status_code == 400
    assert "同名" in resp.json()["detail"]

    # 列表
    resp = await admin_client.get("/api/v1/admin/knowledge-bases")
    assert resp.status_code == 200
    names = [k["name"] for k in resp.json()]
    assert "人事制度库" in names

    # 更新
    resp = await admin_client.patch(f"/api/v1/admin/knowledge-bases/{kb_id}", json={
        "name": "人事制度库V2", "is_public": True,
    })
    assert resp.status_code == 200

    # 有文档时删除拒绝
    admin_id = await _db_user_id("admin_fixture")
    await _seed_doc(kb_id, "考勤制度.txt", admin_id)
    resp = await admin_client.delete(f"/api/v1/admin/knowledge-bases/{kb_id}")
    assert resp.status_code == 400
    assert "还有文档" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_kb_delete_requires_admin(user_client: AsyncClient, default_kb: str):
    """普通用户不能管理知识库"""
    resp = await user_client.post("/api/v1/admin/knowledge-bases", json={"name": "越权建库"})
    assert resp.status_code == 403
    resp = await user_client.delete(f"/api/v1/admin/knowledge-bases/{default_kb}")
    assert resp.status_code == 403


# ════════════════════════════════════════════════════
# 部门管理
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_admin_departments_crud(admin_client: AsyncClient):
    """部门树 CRUD：创建/列表/更新/删除；重名与成环防护"""
    resp = await admin_client.post("/api/v1/admin/departments", json={"name": "人事部"})
    assert resp.status_code == 201
    parent_id = resp.json()["id"]

    resp = await admin_client.post("/api/v1/admin/departments", json={
        "name": "招聘组", "parent_id": parent_id,
    })
    assert resp.status_code == 201
    child_id = resp.json()["id"]

    # 重名拒绝
    resp = await admin_client.post("/api/v1/admin/departments", json={"name": "人事部"})
    assert resp.status_code == 400

    # 树形列表：父下面有子
    resp = await admin_client.get("/api/v1/admin/departments")
    tree = resp.json()
    parent_node = next(d for d in tree if d["id"] == parent_id)
    assert parent_node["children"][0]["id"] == child_id

    # 把自己设为上级 → 400
    resp = await admin_client.patch(f"/api/v1/admin/departments/{parent_id}", json={
        "parent_id": parent_id,
    })
    assert resp.status_code == 400

    # 把自己的子孙设为上级 → 400
    resp = await admin_client.patch(f"/api/v1/admin/departments/{parent_id}", json={
        "parent_id": child_id,
    })
    assert resp.status_code == 400

    # 有子部门时删除父 → 400
    resp = await admin_client.delete(f"/api/v1/admin/departments/{parent_id}")
    assert resp.status_code == 400
    assert "子部门" in resp.json()["detail"]

    # 先删子再删父
    assert (await admin_client.delete(f"/api/v1/admin/departments/{child_id}")).status_code == 200
    assert (await admin_client.delete(f"/api/v1/admin/departments/{parent_id}")).status_code == 200


@pytest.mark.asyncio
async def test_admin_user_departments_binding(admin_client: AsyncClient, user_token: str):
    """用户-部门绑定：设置/查询/替换/无效部门拒绝"""
    resp = await admin_client.post("/api/v1/admin/departments", json={"name": "财务部"})
    dept_id = resp.json()["id"]

    user_id = await _db_user_id("user_fixture")

    # 绑定
    resp = await admin_client.put(f"/api/v1/admin/users/{user_id}/departments", json={
        "department_ids": [dept_id],
    })
    assert resp.status_code == 200

    # 查询
    resp = await admin_client.get(f"/api/v1/admin/users/{user_id}/departments")
    assert [d["id"] for d in resp.json()] == [dept_id]

    # 无效部门 → 400
    resp = await admin_client.put(f"/api/v1/admin/users/{user_id}/departments", json={
        "department_ids": ["not-exist"],
    })
    assert resp.status_code == 400

    # 清空
    resp = await admin_client.put(f"/api/v1/admin/users/{user_id}/departments", json={
        "department_ids": [],
    })
    assert resp.status_code == 200
    resp = await admin_client.get(f"/api/v1/admin/users/{user_id}/departments")
    assert resp.json() == []


# ════════════════════════════════════════════════════
# 库授权与权限矩阵
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_kb_members_grant_and_list(admin_client: AsyncClient, user_token: str):
    """授权：部门+个人两条记录；重复主体拒绝；列表带主体名称"""
    resp = await admin_client.post("/api/v1/admin/departments", json={"name": "IT部"})
    dept_id = resp.json()["id"]
    user_id = await _db_user_id("user_fixture")

    resp = await admin_client.post("/api/v1/admin/knowledge-bases", json={
        "name": "IT运维库", "is_public": False,
    })
    kb_id = resp.json()["id"]

    await _grant_kb(admin_client, kb_id, [
        {"subject_type": "dept", "subject_id": dept_id, "can_read": True, "can_upload": True},
        {"subject_type": "user", "subject_id": user_id, "can_read": True},
    ])

    # 重复主体拒绝
    resp = await admin_client.put(f"/api/v1/admin/knowledge-bases/{kb_id}/members", json={
        "members": [
            {"subject_type": "user", "subject_id": user_id, "can_read": True},
            {"subject_type": "user", "subject_id": user_id, "can_read": True},
        ],
    })
    assert resp.status_code == 400

    # 不存在的主体拒绝
    resp = await admin_client.put(f"/api/v1/admin/knowledge-bases/{kb_id}/members", json={
        "members": [{"subject_type": "user", "subject_id": "ghost", "can_read": True}],
    })
    assert resp.status_code == 400

    # 列表带主体名称
    resp = await admin_client.get(f"/api/v1/admin/knowledge-bases/{kb_id}/members")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    by_type = {m["subject_type"]: m for m in items}
    assert by_type["dept"]["subject_name"] == "IT部"
    assert by_type["user"]["subject_name"] == "user_fixture"


@pytest.mark.asyncio
async def test_upload_permission_matrix(admin_client: AsyncClient, private_kb: str):
    """上传权限矩阵：
    - 无授权用户上传 → 403；指定无权库的列表 → 403
    - 个人授权 can_upload → 上传成功；个人授权覆盖"默认拒绝"
    """
    token = await _register_login(admin_client, "perm_user1")
    user_headers = {"Authorization": f"Bearer {token}"}
    user_id = await _db_user_id("perm_user1")

    files = {"file": ("制度.txt", "内容".encode("utf-8"), "text/plain")}

    # 无授权：上传 403
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files, data={"kb_id": private_kb}, headers=user_headers,
    )
    assert resp.status_code == 403

    # 无授权：指定该库的文档列表 403
    resp = await admin_client.get(
        "/api/v1/knowledge/documents", params={"kb_id": private_kb}, headers=user_headers,
    )
    assert resp.status_code == 403

    # 个人授权 can_upload（schema 规范化：can_read 自动补真）
    await _grant_kb(admin_client, private_kb, [
        {"subject_type": "user", "subject_id": user_id, "can_upload": True},
    ])
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files, data={"kb_id": private_kb}, headers=user_headers,
    )
    assert resp.status_code == 200

    # 能传必然能读
    resp = await admin_client.get(
        "/api/v1/knowledge/documents", params={"kb_id": private_kb}, headers=user_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.asyncio
async def test_department_grant_inherits_to_member(admin_client: AsyncClient, private_kb: str):
    """部门授权：部门成员自动获得库权限（个人行 > 部门行 > 默认拒绝）"""
    resp = await admin_client.post("/api/v1/admin/departments", json={"name": "市场部"})
    dept_id = resp.json()["id"]

    token = await _register_login(admin_client, "perm_user2")
    user_headers = {"Authorization": f"Bearer {token}"}
    user_id = await _db_user_id("perm_user2")

    # 绑部门 + 部门授权可读
    await admin_client.put(f"/api/v1/admin/users/{user_id}/departments", json={
        "department_ids": [dept_id],
    })
    await _grant_kb(admin_client, private_kb, [
        {"subject_type": "dept", "subject_id": dept_id, "can_read": True},
    ])

    # 部门成员可读
    resp = await admin_client.get(
        "/api/v1/knowledge/documents", params={"kb_id": private_kb}, headers=user_headers,
    )
    assert resp.status_code == 200

    # 但部门只授了读：上传仍 403
    files = {"file": ("制度.txt", "内容".encode("utf-8"), "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files, data={"kb_id": private_kb}, headers=user_headers,
    )
    assert resp.status_code == 403

    # 个人行覆盖：给个人加传权限
    await _grant_kb(admin_client, private_kb, [
        {"subject_type": "dept", "subject_id": dept_id, "can_read": True},
        {"subject_type": "user", "subject_id": user_id, "can_upload": True},
    ])
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files, data={"kb_id": private_kb}, headers=user_headers,
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_document_delete_permission_rules(admin_client: AsyncClient, private_kb: str):
    """删除文档权限：can_upload 只能删自己的；can_manage 任意删；无权限 403"""
    admin_id = await _db_user_id("admin_fixture")

    # 管理员传两份文档
    doc_admin = await _seed_doc(private_kb, "管理员文档.txt", admin_id)

    # 用户A：can_upload
    token_a = await _register_login(admin_client, "perm_user3")
    user_a_headers = {"Authorization": f"Bearer {token_a}"}
    user_a_id = await _db_user_id("perm_user3")
    await _grant_kb(admin_client, private_kb, [
        {"subject_type": "user", "subject_id": user_a_id, "can_upload": True},
    ])

    # 删别人的文档 → 403
    resp = await admin_client.delete(
        f"/api/v1/knowledge/documents/{doc_admin}", headers=user_a_headers,
    )
    assert resp.status_code == 403

    # 自己传一份再删 → 200
    files = {"file": ("我的文档.txt", "内容".encode("utf-8"), "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files, data={"kb_id": private_kb}, headers=user_a_headers,
    )
    assert resp.status_code == 200
    doc_own_id = resp.json()["id"]
    resp = await admin_client.delete(
        f"/api/v1/knowledge/documents/{doc_own_id}", headers=user_a_headers,
    )
    assert resp.status_code == 200

    # 用户B：can_manage → 可以删别人的
    token_b = await _register_login(admin_client, "perm_user4")
    user_b_headers = {"Authorization": f"Bearer {token_b}"}
    user_b_id = await _db_user_id("perm_user4")
    await _grant_kb(admin_client, private_kb, [
        {"subject_type": "user", "subject_id": user_b_id, "can_manage": True},
    ])
    resp = await admin_client.delete(
        f"/api/v1/knowledge/documents/{doc_admin}", headers=user_b_headers,
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_category_per_kb_isolation(admin_client: AsyncClient, default_kb: str, private_kb: str):
    """分类库隔离：不同库可建同名分类；跨库挂分类被拒；非库管理者建分类 403"""
    # 两个库各自建同名分类（旧全局唯一约束下这会失败）
    resp = await admin_client.post("/api/v1/admin/knowledge-bases", json={"name": "财务库"})
    kb2 = resp.json()["id"]
    for kb in (default_kb, private_kb, kb2):
        resp = await admin_client.post("/api/v1/knowledge/categories", json={
            "name": "报销", "kb_id": kb,
        })
        assert resp.status_code == 201

    # 各库分类树隔离：指定 kb 查询只返回该库分类
    resp = await admin_client.get("/api/v1/knowledge/categories", params={"kb_id": default_kb})
    default_cats = resp.json()
    assert len(default_cats) == 1
    assert default_cats[0]["name"] == "报销"

    # 跨库挂分类：上传到 default_kb 但传 private_kb 的分类 → 400
    resp = await admin_client.get("/api/v1/knowledge/categories", params={"kb_id": private_kb})
    foreign_cat_id = resp.json()[0]["id"]
    files = {"file": ("文档.txt", "内容".encode("utf-8"), "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files, data={"kb_id": default_kb, "category_id": foreign_cat_id},
    )
    assert resp.status_code == 400
    assert "不属于该知识库" in resp.json()["detail"]

    # 普通用户建分类（公共库无管理权）→ 403
    token = await _register_login(admin_client, "perm_user5")
    resp = await admin_client.post(
        "/api/v1/knowledge/categories",
        json={"name": "越权分类", "kb_id": default_kb},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_personal_grant_does_not_leak_to_other_users(admin_client: AsyncClient, private_kb: str):
    """隔离回归（质量审查 HIGH 修复）：给用户 A 的个人授权，绝不能泄漏给用户 B。

    曾有一个条件拼接 bug：个人授权条件写成「type=user OR id=本人」两个独立条件，
    导致任何用户的个人授权行被全系统所有人继承。此测试专门拦截该回归。
    """
    from app.services import kb_service
    from app.models.user import User
    from sqlalchemy import select

    token_a = await _register_login(admin_client, "perm_user_a")
    token_b = await _register_login(admin_client, "perm_user_b")
    user_a_id = await _db_user_id("perm_user_a")
    user_b_id = await _db_user_id("perm_user_b")

    # 只给 A 个人授权
    await _grant_kb(admin_client, private_kb, [
        {"subject_type": "user", "subject_id": user_a_id, "can_manage": True},
    ])

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.id == user_a_id))
        user_a = result.scalar_one()
        result = await db.execute(select(User).where(User.id == user_b_id))
        user_b = result.scalar_one()

        perms_a = await kb_service.get_user_kb_perms(db, user_a)
        perms_b = await kb_service.get_user_kb_perms(db, user_b)

    # A 有管理权（含读）
    assert perms_a.get(private_kb, {}).get("can_manage") is True
    assert perms_a.get(private_kb, {}).get("can_read") is True
    # B 完全不继承 A 的个人授权（无条目时返回 None，同样视为无权限）
    assert perms_b.get(private_kb, {}).get("can_read") is not True
    assert perms_b.get(private_kb, {}).get("can_upload") is not True
    assert perms_b.get(private_kb, {}).get("can_manage") is not True


@pytest.mark.asyncio
async def test_visible_doc_scope_isolation(admin_client: AsyncClient, default_kb: str, private_kb: str):
    """服务层可见文档隔离：部门授权了哪个库，就只能看到哪个库的文档"""
    from app.services import kb_service
    from app.models.user import User
    from sqlalchemy import select

    resp = await admin_client.post("/api/v1/admin/departments", json={"name": "行政部"})
    dept_id = resp.json()["id"]

    # 公共库放一份文档（全员可见）、私有库放一份（仅授权可见）
    admin_id = await _db_user_id("admin_fixture")
    doc_public = await _seed_doc(default_kb, "公共文档.txt", admin_id)
    doc_private = await _seed_doc(private_kb, "机密文档.txt", admin_id)

    token = await _register_login(admin_client, "perm_user6")
    user_id = await _db_user_id("perm_user6")

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()

        # 未绑定部门、未授权：只能看到公共库文档
        visible = await kb_service.get_visible_doc_ids(db, user)
        assert doc_public in visible
        assert doc_private not in visible

        # 绑定部门并给部门授权私有库后：两个都能看到
        from app.models.user import User
        from app.models.document import Document
        from app.models.knowledge_base import KbMember, UserDepartment

        db.add(UserDepartment(user_id=user_id, department_id=dept_id))
        db.add(KbMember(
            kb_id=private_kb, subject_type="dept", subject_id=dept_id,
            can_read=True,
        ))
        await db.commit()

        result = await db.execute(select(Document.id))
        all_ids = {row[0] for row in result.all()}
        visible = await kb_service.get_visible_doc_ids(db, user)
        assert doc_public in visible and doc_private in visible
        assert all_ids == set(visible)  # 库就两个，全部可见


@pytest.mark.asyncio
async def test_chat_no_visible_kb_gets_polite_message(client: AsyncClient):
    """问答：用户没有任何可读知识库时，得到权限提示而非 AI 回答（不调外部 LLM）"""
    token = await _register_login(client, "no_kb_user")

    events: dict[str, list[dict]] = {}
    async with client.stream(
        "POST", "/api/v1/chat/stream",
        json={"message": "年假怎么请", "stream": True},
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        assert resp.status_code == 200
        event_name = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: ") and event_name:
                events.setdefault(event_name, []).append(json.loads(line[len("data: "):]))

    tokens = [d["content"] for d in events.get("token", [])]
    full_text = "".join(tokens)
    assert "没有可访问的知识库" in full_text
    assert "done" in events
