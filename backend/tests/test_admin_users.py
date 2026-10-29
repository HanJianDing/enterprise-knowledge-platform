"""阶段3 测试：管理员建号 / CSV 批量导入 / editor 角色 / 审计日志"""
import asyncio

import pytest
from httpx import AsyncClient

from tests.conftest import create_user_in_db


async def _wait_audits(admin_client: AsyncClient, params: dict, expect: int,
                       timeout: float = 5.0) -> list[dict]:
    """审计写入是后台任务（最终一致）：轮询直到出现至少 expect 条匹配记录"""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        resp = await admin_client.get("/api/v1/admin/audit-logs", params=params)
        items = resp.json()["items"]
        if len(items) >= expect:
            return items
        if asyncio.get_event_loop().time() > deadline:
            return items  # 超时返回当前值，断言自然失败给出诊断
        await asyncio.sleep(0.2)


# ════════════════════════════════════════════════════
# 管理员建号
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_admin_create_user(admin_client: AsyncClient, user_token: str):
    """管理员建号：201、首登强制改密、绑部门"""
    # 建部门
    resp = await admin_client.post("/api/v1/admin/departments", json={"name": "测试部"})
    dept_id = resp.json()["id"]

    resp = await admin_client.post("/api/v1/admin/users", json={
        "username": "new_staff", "email": "staff@test.com",
        "password": "init123456", "role": "user",
        "department_ids": [dept_id],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "new_staff"
    assert data["must_change_password"] is True
    assert "password" not in data

    # 新员工登录 → must_change_password=True
    resp = await admin_client.post("/api/v1/auth/login", json={
        "username": "new_staff", "password": "init123456"
    })
    assert resp.json()["must_change_password"] is True

    # 部门已绑定
    resp = await admin_client.get(f"/api/v1/admin/users/{data['id']}/departments")
    assert [d["id"] for d in resp.json()] == [dept_id]

    # 普通用户不能建号
    resp = await admin_client.post("/api/v1/admin/users", json={
        "username": "x", "email": "x@test.com", "password": "Abc12345",
    }, headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_create_user_duplicate_and_role_validation(admin_client: AsyncClient):
    """建号：用户名/邮箱重复 409；非法角色 400"""
    resp = await admin_client.post("/api/v1/admin/users", json={
        "username": "dup_staff", "email": "dup@test.com", "password": "Abc12345",
    })
    assert resp.status_code == 201

    resp = await admin_client.post("/api/v1/admin/users", json={
        "username": "dup_staff", "email": "other@test.com", "password": "Abc12345",
    })
    assert resp.status_code == 409
    assert "用户名已存在" in resp.json()["detail"]

    resp = await admin_client.post("/api/v1/admin/users", json={
        "username": "bad_role", "email": "bad@test.com", "password": "Abc12345",
        "role": "agent",  # 客服角色已随阶段2移除
    })
    assert resp.status_code == 422


# ════════════════════════════════════════════════════
# CSV 批量导入
# ════════════════════════════════════════════════════

CSV_CONTENT = (
    "用户名,邮箱,初始密码,角色,部门\n"
    "staff_a,a@test.com,init111111,user,\n"
    "staff_b,b@test.com,init222222,editor,\n"
    "staff_c,c@test.com,init333333,user,测试部\n"
    "bad_row,,nopass,user,\n"
    "dup_a,a@test.com,init999999,user,\n"
    "bad_dept,d@test.com,init444444,user,不存在的部门\n"
).encode("utf-8")


@pytest.mark.asyncio
async def test_admin_import_users_csv(admin_client: AsyncClient):
    """CSV 导入：正常行创建、空行/重复/坏部门记错误不中断"""
    resp = await admin_client.post("/api/v1/admin/departments", json={"name": "测试部"})
    assert resp.status_code == 201

    resp = await admin_client.post(
        "/api/v1/admin/users/import",
        files={"file": ("staff.csv", CSV_CONTENT, "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 3
    reasons = {(e["row"], e["username"]) for e in data["errors"]}
    assert len(data["errors"]) == 3
    # 行号从 2 开始（表头第 1 行）
    assert (5, "bad_row") in reasons  # 缺邮箱/密码的行记原用户名
    assert (6, "dup_a") in reasons
    assert (7, "bad_dept") in reasons

    # 导入的用户可登录且首登强制改密
    resp = await admin_client.post("/api/v1/auth/login", json={
        "username": "staff_a", "password": "init111111"
    })
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True

    # staff_b 是 editor
    from app.infrastructure.database import async_session_factory
    from app.models.user import User
    from sqlalchemy import select
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.username == "staff_b"))
        assert result.scalar_one().role == "editor"


@pytest.mark.asyncio
async def test_admin_import_bad_header(admin_client: AsyncClient):
    """CSV 表头缺失 → 400"""
    resp = await admin_client.post(
        "/api/v1/admin/users/import",
        files={"file": ("bad.csv", b"a,b,c\n1,2,3\n", "text/csv")},
    )
    assert resp.status_code == 400
    assert "表头" in resp.json()["detail"]


# ════════════════════════════════════════════════════
# editor 角色语义
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_editor_upload_scoped_to_visible_kbs(admin_client: AsyncClient, private_kb: str):
    """editor 语义：可见库内必然可传（内容维护身份）；看不见的私有库不可读也不可传；管理权仍按授权"""
    await create_user_in_db("editor1", "editor1@test.com", role="editor")
    resp = await admin_client.post("/api/v1/auth/login", json={
        "username": "editor1", "password": "123456"
    })
    H = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    # 未授权私有库：上传 403（不可见不可传）、列表 403
    files = {"file": ("制度.txt", "内容".encode("utf-8"), "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files, data={"kb_id": private_kb}, headers=H,
    )
    assert resp.status_code == 403
    resp = await admin_client.get(
        "/api/v1/knowledge/documents", params={"kb_id": private_kb}, headers=H,
    )
    assert resp.status_code == 403

    # 授权 read 后：editor 自动可传（可见库内必然可传）
    from app.infrastructure.database import async_session_factory
    from app.models.knowledge_base import KbMember
    from app.models.user import User
    from sqlalchemy import select
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.username == "editor1"))
        editor_id = result.scalar_one().id
        db.add(KbMember(
            kb_id=private_kb, subject_type="user", subject_id=editor_id,
            can_read=True,
        ))
        await db.commit()

    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files, data={"kb_id": private_kb}, headers=H,
    )
    assert resp.status_code == 200

    # 不能建库/授权（admin 接口）
    resp = await admin_client.post("/api/v1/admin/knowledge-bases", headers=H, json={
        "name": "越权库",
    })
    assert resp.status_code == 403


# ════════════════════════════════════════════════════
# 审计日志
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_audit_logs_recorded_and_queryable(admin_client: AsyncClient):
    """关键操作留痕：建库/建用户/登录可查，支持按 action/username 过滤"""
    # 触发一批审计事件
    resp = await admin_client.post("/api/v1/admin/knowledge-bases", json={
        "name": "审计测试库", "is_public": False,
    })
    assert resp.status_code == 201
    await admin_client.post("/api/v1/admin/users", json={
        "username": "audit_staff", "email": "audit@test.com", "password": "Abc12345",
    })
    # 触发登录审计事件——注意：admin 再次登录会顶号（sv+1），
    # 夹具里的旧 token 随之失效，必须换成新 token
    resp = await admin_client.post("/api/v1/auth/login", json={
        "username": "admin_fixture", "password": "123456"
    })
    admin_client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    # 全量查询（后台任务最终一致：轮询等待落库）
    items = await _wait_audits(admin_client, {}, 3)
    actions = {item["action"] for item in items}
    assert {"kb.create", "user.create", "user.login"} <= actions

    # 按 action 过滤
    items = await _wait_audits(admin_client, {"action": "kb.create"}, 1)
    assert all(item["action"] == "kb.create" for item in items)

    # 按 username 过滤
    items = await _wait_audits(admin_client, {"username": "admin_fixture"}, 1)
    assert all(item["username"] == "admin_fixture" for item in items)

    # 普通用户不能看审计
    await create_user_in_db("peeper", "peeper@test.com")
    resp = await admin_client.post("/api/v1/auth/login", json={
        "username": "peeper", "password": "123456"
    })
    resp = await admin_client.get(
        "/api/v1/admin/audit-logs",
        headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_audit_document_operations(admin_client: AsyncClient, default_kb: str):
    """传删文档留痕"""
    files = {"file": ("审计文档.txt", "审计内容".encode("utf-8"), "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files, data={"kb_id": default_kb},
    )
    assert resp.status_code == 200
    doc_id = resp.json()["id"]

    # 等后台处理任务结束再删（上传任务是异步的，直接删会与任务抢 Chroma 打开锁）
    for _ in range(30):
        r = await admin_client.get(
            "/api/v1/knowledge/documents", params={"kb_id": default_kb}
        )
        items = r.json()["items"]
        if items and items[0]["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.5)

    resp = await admin_client.delete(f"/api/v1/knowledge/documents/{doc_id}")
    assert resp.status_code == 200

    uploads = await _wait_audits(
        admin_client, {"action": "document.upload"}, 1
    )
    assert uploads and uploads[0]["detail"]["filename"] == "审计文档.txt"

    deletes = await _wait_audits(
        admin_client, {"action": "document.delete"}, 1
    )
    assert len(deletes) >= 1
