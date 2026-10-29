"""对话模块测试"""
from tests.conftest import create_user_in_db
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_chat_stream_requires_auth(client: AsyncClient):
    """测试未认证不能发送消息"""
    response = await client.post("/api/v1/chat/stream", json={
        "message": "测试消息", "stream": True
    })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chat_stream_empty_message(admin_client: AsyncClient):
    """测试空消息被拒绝"""
    response = await admin_client.post("/api/v1/chat/stream", json={
        "message": "", "stream": True
    })
    assert response.status_code == 422  # Pydantic 校验: min_length=1


@pytest.mark.asyncio
async def test_chat_stream_message_too_long(admin_client: AsyncClient):
    """上线安全修复：超过 2000 字的消息被拒绝（防超长文本烧 token）"""
    response = await admin_client.post("/api/v1/chat/stream", json={
        "message": "长" * 2001, "stream": True
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_conversations_list(admin_client: AsyncClient):
    """测试获取会话列表"""
    response = await admin_client.get("/api/v1/conversations?page=1&page_size=10")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert data["page"] == 1


@pytest.mark.asyncio
async def test_conversation_not_found(admin_client: AsyncClient):
    """测试获取不存在的会话"""
    response = await admin_client.get("/api/v1/conversations/nonexistent-id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_conversation_isolation(client: AsyncClient, admin_token: str):
    """测试用户只能看到自己的会话"""
    # 注册两个用户
    await create_user_in_db("user_a", "a@test.com", "123456")
    await create_user_in_db("user_b", "b@test.com", "123456")

    # 用户A登录并创建会话
    resp = await client.post("/api/v1/auth/login", json={
        "username": "user_a", "password": "123456"
    })
    token_a = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token_a}"

    # 发送消息创建会话
    import asyncio
    async def consume_stream():
        async with client.stream("POST", "/api/v1/chat/stream", json={
            "message": "用户A的测试问题", "stream": True
        }) as resp:
            await resp.aread()

    # 60s：真实 LLM/检索调用受外部 API 延迟影响，15s 太紧会造成误报超时
    await asyncio.wait_for(consume_stream(), timeout=60)

    # 用户A的会话列表应该有1条
    resp = await client.get("/api/v1/conversations")
    data_a = resp.json()
    assert data_a["total"] >= 1
    conv_id = data_a["items"][0]["id"]

    # 用户B登录，尝试访问用户A的会话
    resp = await client.post("/api/v1/auth/login", json={
        "username": "user_b", "password": "123456"
    })
    token_b = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token_b}"

    # 用户B访问用户A的会话应返回404
    resp = await client.get(f"/api/v1/conversations/{conv_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_conversation(admin_client: AsyncClient):
    """测试删除会话"""
    # 先创建一个会话
    import asyncio
    async def consume_stream():
        async with admin_client.stream("POST", "/api/v1/chat/stream", json={
            "message": "将被删除的测试对话", "stream": True
        }) as resp:
            await resp.aread()

    # 60s：真实 LLM/检索调用受外部 API 延迟影响，15s 太紧会造成误报超时
    await asyncio.wait_for(consume_stream(), timeout=60)

    # 获取会话列表
    resp = await admin_client.get("/api/v1/conversations")
    data = resp.json()
    assert data["total"] >= 1
    conv_id = data["items"][0]["id"]

    # 删除会话
    resp = await admin_client.delete(f"/api/v1/conversations/{conv_id}")
    assert resp.status_code == 200
    assert "已删除" in resp.json()["message"]

    # 确认已删除
    resp = await admin_client.get(f"/api/v1/conversations/{conv_id}")
    assert resp.status_code == 404


# ── 批次F 挂清单收敛：规则直出拼装回归（质检 P2 二次修复钉住）──

def _hit(**kw):
    """构造 rule_hit（业务类型/数值/单位/主项/依据文本）"""
    base = {"business_type": "报销", "value": 8000.0, "unit": "元",
            "approver": "财务总监", "action": "审批",
            "rule_text": "5000 元以上由财务总监审批"}
    base.update(kw)
    return base


class TestRuleDirectAnswerFormat:
    """_format_rule_direct_answer 直出文本拼装（确定性路径，跳过 LLM）"""

    def _fmt(self, hit, approve, others):
        from app.services.chat_service import _format_rule_direct_answer
        return _format_rule_direct_answer(hit, approve, others)

    def test_single_approval_step(self):
        """单步审批链：只输出主项"""
        text = self._fmt(_hit(), [], [])
        assert text.startswith("报销8000元由财务总监审批。")
        assert "依据：5000 元以上由财务总监审批。" in text

    def test_approval_main_with_other_approval_step(self):
        """质检 P2 二次修复：主项审批类+链含低档审批类动作（"5000 以上
        财务总监审批，10000 以上总经理批准"）——主项曾被静默漏掉，
        现无条件拼入；低档审批在前、主项最高档在后（审批流程顺序）"""
        hit = _hit(approver="总经理", action="批准",
                   rule_text="10000 元以上报总经理批准")
        approve = [{"approver": "财务总监", "action": "审批"}]
        text = self._fmt(hit, approve, [])
        assert text.startswith("报销8000元由财务总监审批，总经理批准。")

    def test_filing_main_with_approval_step(self):
        """主项备案类：审批步骤在前，主项加"另需"（请假 10 天验收用例）"""
        hit = _hit(business_type="请假", value=10.0, unit="天",
                   approver="人事部", action="备案",
                   rule_text="七天以上报人事部备案")
        approve = [{"approver": "部门负责人", "action": "审批",
                    "rule_text": "三天以上由部门负责人审批"}]
        text = self._fmt(hit, approve, [])
        assert text.startswith("请假10天由部门负责人审批，另需人事部备案。")
        # 挂清单 #1 修复：依据行拼完整链（主项原文+其他环节原文并列）
        assert "依据：七天以上报人事部备案；三天以上由部门负责人审批。" in text

    def test_approval_main_with_filing_step(self):
        """主项审批类+备案步骤：备案以"另需"后缀全量输出（质检 P2 修复）"""
        hit = _hit(approver="部门负责人", action="审批",
                   rule_text="1000 至 5000 元由部门负责人审批")
        others = [{"approver": "人事部", "action": "备案"}]
        text = self._fmt(hit, [], others)
        assert text.startswith("报销8000元由部门负责人审批，另需人事部备案。")

    def test_integer_value_no_decimal(self):
        """整数值不显示小数点（:g 格式）"""
        hit = _hit(value=3000.0)
        text = self._fmt(hit, [], [])
        assert text.startswith("报销3000元由财务总监审批。")
