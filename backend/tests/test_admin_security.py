"""产品化 C 测试：密码策略 / 管理员重置密码 / auditor 角色 / 审计导出 CSV"""
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.infrastructure.database import async_session_factory
from app.models.audit import AuditLog
from app.models.user import User
from tests.conftest import create_user_in_db


async def _login(client: AsyncClient, username: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={
        "username": username, "password": password,
    })
    assert resp.status_code == 200
    return resp.json()["access_token"]


# ════════════════════════════════════════════════════
# 密码策略（产品化 C2）
# ════════════════════════════════════════════════════

class TestPasswordPolicy:
    @pytest.mark.asyncio
    async def test_create_user_weak_password_rejected(self, admin_client: AsyncClient):
        """建号：6 位纯数字密码被 422 拒绝"""
        resp = await admin_client.post("/api/v1/admin/users", json={
            "username": "weak_pw", "email": "weak@test.com", "password": "123456",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_user_strong_password_ok(self, admin_client: AsyncClient):
        """建号：8 位字母+数字通过"""
        resp = await admin_client.post("/api/v1/admin/users", json={
            "username": "strong_pw", "email": "strong@test.com", "password": "Abc12345",
        })
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_csv_weak_password_row_error(self, admin_client: AsyncClient):
        """CSV 导入：弱密码行记行级错误（不中断其他行）"""
        content = (
            "用户名,邮箱,初始密码,角色,部门\n"
            "csv_ok,ok@test.com,Abc12345,user,\n"
            "csv_weak,weak@test.com,123456,user,\n"
        ).encode("utf-8")
        resp = await admin_client.post(
            "/api/v1/admin/users/import",
            files={"file": ("staff.csv", content, "text/csv")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == 1
        assert (3, "csv_weak") in {(e["row"], e["username"]) for e in data["errors"]}


class TestAdminResetPassword:
    @pytest.mark.asyncio
    async def test_reset_password_flow(self, admin_client: AsyncClient):
        """管理员重置：改密标记置位 + 会话版本 +1（踢旧会话）+ 审计留痕"""
        await create_user_in_db("victim", "victim@test.com")
        async with async_session_factory() as db:
            victim = (await db.execute(
                select(User).where(User.username == "victim")
            )).scalar_one()
            old_sv = victim.session_version

        resp = await admin_client.post(
            f"/api/v1/admin/users/{victim.id}/reset-password",
            json={"new_password": "NewPass123"},
        )
        assert resp.status_code == 200

        async with async_session_factory() as db:
            refreshed = (await db.execute(
                select(User).where(User.username == "victim")
            )).scalar_one()
        assert refreshed.must_change_password is True
        assert refreshed.session_version == old_sv + 1

        async with async_session_factory() as db:
            logs = (await db.execute(
                select(AuditLog).where(AuditLog.action == "user.password_reset")
            )).scalars().all()
        assert len(logs) == 1
        assert logs[0].detail["target_username"] == "victim"

    @pytest.mark.asyncio
    async def test_reset_password_weak_rejected(self, admin_client: AsyncClient):
        await create_user_in_db("victim2", "victim2@test.com")
        async with async_session_factory() as db:
            victim = (await db.execute(
                select(User).where(User.username == "victim2")
            )).scalar_one()
        resp = await admin_client.post(
            f"/api/v1/admin/users/{victim.id}/reset-password",
            json={"new_password": "123456"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_reset_other_admin_rejected(self, admin_client: AsyncClient):
        """不能重置其他管理员的密码（防横向越权）"""
        await create_user_in_db("admin2", "admin2@test.com", role="admin")
        async with async_session_factory() as db:
            admin2 = (await db.execute(
                select(User).where(User.username == "admin2")
            )).scalar_one()
        resp = await admin_client.post(
            f"/api/v1/admin/users/{admin2.id}/reset-password",
            json={"new_password": "NewPass123"},
        )
        assert resp.status_code == 403


# ════════════════════════════════════════════════════
# auditor 角色（产品化 C5）
# ════════════════════════════════════════════════════

class TestAuditorRole:
    @pytest.mark.asyncio
    async def test_auditor_can_view_audit_logs(self, client: AsyncClient):
        await create_user_in_db("auditor1", "auditor1@test.com", role="auditor")
        token = await _login(client, "auditor1", "123456")
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.get("/api/v1/admin/audit-logs", headers=headers)
        assert resp.status_code == 200
        resp = await client.get("/api/v1/admin/audit-logs/export", headers=headers)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_auditor_cannot_access_admin_pages(self, client: AsyncClient):
        """审计员只能看审计：用户列表/知识库管理均为 403"""
        await create_user_in_db("auditor2", "auditor2@test.com", role="auditor")
        token = await _login(client, "auditor2", "123456")
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.get("/api/v1/admin/users", headers=headers)
        assert resp.status_code == 403
        resp = await client.get("/api/v1/admin/dashboard", headers=headers)
        assert resp.status_code == 403
        resp = await client.post("/api/v1/admin/eval/retrieval-test",
                                 json={"question": "年假", "top_k": 6}, headers=headers)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_normal_user_cannot_view_audit_logs(self, client: AsyncClient):
        """普通员工看不了审计日志"""
        await create_user_in_db("normal_peeper", "np@test.com")
        token = await _login(client, "normal_peeper", "123456")
        resp = await client.get("/api/v1/admin/audit-logs",
                                headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403


# ════════════════════════════════════════════════════
# 审计导出 CSV（产品化 C3）
# ════════════════════════════════════════════════════

class TestAuditExport:
    @pytest.mark.asyncio
    async def test_export_csv_format(self, admin_client: AsyncClient):
        """导出 CSV：BOM + 表头 + 行内容（seed 一条审计后导出）"""
        from app.services import audit_service

        async with async_session_factory() as db:
            admin = (await db.execute(
                select(User).where(User.role == "admin").limit(1)
            )).scalar_one()
            await audit_service.record_audit(
                db, admin.id, admin.username, "user.login",
                detail={"note": "导出测试"},
            )
            await db.commit()

        resp = await admin_client.get("/api/v1/admin/audit-logs/export")
        assert resp.status_code == 200
        text = resp.content.decode("utf-8")
        assert text.startswith("﻿")  # UTF-8 BOM
        assert "时间,用户,操作" in text
        assert "user.login" in text
        assert "导出测试" in text
        assert "attachment" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_export_csv_formula_injection_neutralized(self, admin_client: AsyncClient):
        """CSV 注入防护：以 = 开头的用户可控文本加单引号前缀（Excel 不当公式执行）"""
        from app.services import audit_service

        async with async_session_factory() as db:
            admin = (await db.execute(
                select(User).where(User.role == "admin").limit(1)
            )).scalar_one()
            await audit_service.record_audit(
                db, admin.id, "=cmd|'/C calc'!A0", "user.login",
                detail={"note": "注入测试"},
            )
            await db.commit()

        resp = await admin_client.get("/api/v1/admin/audit-logs/export")
        text = resp.content.decode("utf-8")
        # 原样 =cmd 不应出现在单元格开头（被 ' 前缀中和）；前缀形式出现
        assert "'=cmd|'/C calc'!A0" in text
