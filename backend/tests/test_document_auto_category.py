"""批次F 验收 3.15/3.16：分类自动建立 + 列表分类筛选"""
import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.infrastructure.database import async_session_factory
from app.models.document import Document, KnowledgeCategory
from app.models.user import User
from app.services.document_service import _extract_theme, _form_child_for


class TestExtractTheme:
    """文件名 → 主题名提取"""

    def test_basic_suffix_strip(self):
        assert _extract_theme("验收测试-会议室管理制度.txt") == "会议室"
        assert _extract_theme("车辆使用管理规定.docx") == "车辆使用"
        assert _extract_theme("访客登记管理制度.pdf") == "访客登记"

    def test_flow_table_faq_suffix(self):
        assert _extract_theme("办公用品申领流程.md") == "办公用品"
        # "价格表"整体剥掉 → "办公用品"（与既有主题名对齐）
        assert _extract_theme("办公用品价格表.xlsx") == "办公用品"
        assert _extract_theme("会议室使用登记表.csv") == "会议室使用"

    def test_longest_suffix_first(self):
        # "管理制度" 长于 "制度"：先剥长后缀，否则残留"管理"
        assert _extract_theme("考勤管理制度.txt") == "考勤"

    def test_bare_suffix_word_kept(self):
        # 词干与后缀相同（"制度"）→ 不去除，本身是可用主题名
        assert _extract_theme("制度.txt") == "制度"
        assert _extract_theme("流程.md") == "流程"

    def test_plain_name_kept(self):
        assert _extract_theme("楼层平面示意图.jpg") == "楼层平面示意图"


class TestFormChild:
    """文档形态 → 形态子分类"""

    def test_table_to_form(self):
        assert _form_child_for("价格表.xlsx", "table") == "表单"

    def test_faq(self):
        assert _form_child_for("常见问题.md", "faq") == "FAQ"
        assert _form_child_for("员工FAQ.md", "long_text") == "FAQ"

    def test_flow_keyword(self):
        assert _form_child_for("请假流程.md", "long_text") == "流程"

    def test_default_to_policy(self):
        assert _form_child_for("考勤管理制度.txt", "long_text") == "制度"


@pytest.mark.asyncio
async def test_upload_without_category_auto_creates(
    admin_client: AsyncClient, default_kb: str
):
    """3.15：未选分类上传 → 主题父分类 + 四形态子分类自动建立，文档挂形态子分类"""
    files = {"file": ("验收测试-会议室管理制度.txt",
                      "会议室管理制度测试内容。".encode("utf-8"), "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload", files=files, data={"kb_id": default_kb}
    )
    assert resp.status_code == 200

    async with async_session_factory() as db:
        doc = (await db.execute(
            select(Document).where(Document.original_name == "验收测试-会议室管理制度.txt")
        )).scalar_one()
        assert doc.category_id is not None
        cat = (await db.execute(
            select(KnowledgeCategory).where(KnowledgeCategory.id == doc.category_id)
        )).scalar_one()
        assert cat.name == "制度"  # 长文本 → 制度
        parent = (await db.execute(
            select(KnowledgeCategory).where(KnowledgeCategory.id == cat.parent_id)
        )).scalar_one()
        assert parent.name == "会议室"  # 主题提取：剥"管理制度"
        # 四形态子分类齐
        forms = (await db.execute(
            select(KnowledgeCategory.name).where(
                KnowledgeCategory.parent_id == parent.id
            )
        )).scalars().all()
        assert set(forms) == {"制度", "流程", "表单", "FAQ"}
        # 流水事件记录自动分类
        assert any(e["stage"] == "auto_category" for e in (doc.process_log or []))


@pytest.mark.asyncio
async def test_upload_without_category_reuses_existing_theme(
    admin_client: AsyncClient, default_kb: str
):
    """3.15：主题父分类已存在 → 复用，不重复创建"""
    async with async_session_factory() as db:
        parent = KnowledgeCategory(name="会议室", kb_id=default_kb)
        db.add(parent)
        await db.flush()
        for form in ("制度", "流程", "表单", "FAQ"):
            db.add(KnowledgeCategory(name=form, kb_id=default_kb, parent_id=parent.id))
        await db.commit()
        parent_id = parent.id

    files = {"file": ("验收测试-会议室管理制度.txt",
                      "另一份会议室制度。".encode("utf-8"), "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload", files=files, data={"kb_id": default_kb}
    )
    assert resp.status_code == 200

    async with async_session_factory() as db:
        parents = (await db.execute(
            select(KnowledgeCategory).where(
                KnowledgeCategory.kb_id == default_kb,
                KnowledgeCategory.name == "会议室",
                KnowledgeCategory.parent_id.is_(None),
            )
        )).scalars().all()
        assert len(parents) == 1  # 复用而非重复创建
        doc = (await db.execute(
            select(Document).where(Document.original_name == "验收测试-会议室管理制度.txt")
        )).scalar_one()
        cat = (await db.execute(
            select(KnowledgeCategory).where(KnowledgeCategory.id == doc.category_id)
        )).scalar_one()
        assert cat.parent_id == parent_id


@pytest.mark.asyncio
async def test_reupload_same_name_keeps_category(
    admin_client: AsyncClient, default_kb: str
):
    """版本重传沿用旧版分类（版本链内分类一致），不新建主题"""
    async def _upload():
        files = {"file": ("验收测试-访客登记管理制度.txt",
                          "访客登记制度 v1。".encode("utf-8"), "text/plain")}
        r = await admin_client.post(
            "/api/v1/knowledge/documents/upload",
            files=files, data={"kb_id": default_kb},
        )
        assert r.status_code == 200

    async def _wait_processed():
        """等后台处理任务结束（conftest mock 立即标 completed，但仍异步）"""
        import time

        from app.services import document_service as ds

        deadline = time.monotonic() + 5
        while ds._in_flight_doc_ids and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    await _upload()
    await _wait_processed()
    async with async_session_factory() as db:
        v1 = (await db.execute(
            select(Document).where(Document.original_name == "验收测试-访客登记管理制度.txt")
        )).scalar_one()
        v1_cat = v1.category_id

    await _upload()  # 同名重传 → v2
    await _wait_processed()
    async with async_session_factory() as db:
        v2 = (await db.execute(
            select(Document).where(
                Document.original_name == "验收测试-访客登记管理制度.txt",
                Document.status == "completed",
            )
        )).scalar_one()
        assert v2.category_id == v1_cat


@pytest.mark.asyncio
async def test_list_filter_by_parent_category_includes_children(
    admin_client: AsyncClient, default_kb: str
):
    """3.16：按主题父分类筛选 → 含其全部形态子分类的文档"""
    async with async_session_factory() as db:
        admin = (await db.execute(
            select(User).where(User.role == "admin").limit(1)
        )).scalar_one()
        parent = KnowledgeCategory(name="食堂", kb_id=default_kb)
        db.add(parent)
        await db.flush()
        kids = {}
        for form in ("制度", "流程", "表单", "FAQ"):
            k = KnowledgeCategory(name=form, kb_id=default_kb, parent_id=parent.id)
            db.add(k)
            kids[form] = k
        await db.flush()
        # 两个挂"制度"、一个挂"表单"、一个其他主题
        for i, form in enumerate(("制度", "制度", "表单")):
            db.add(Document(
                id=f"catdoc{i}", filename=f"catdoc{i}.txt",
                original_name=f"食堂文档{i}.txt", file_type="txt", file_size=10,
                storage_path=f"catdoc{i}.txt", kb_id=default_kb,
                category_id=kids[form].id, content_type="long_text",
                status="completed", uploaded_by=admin.id, version_no=1,
            ))
        db.add(Document(
            id="catdoc9", filename="catdoc9.txt", original_name="车辆文档.txt",
            file_type="txt", file_size=10, storage_path="catdoc9.txt",
            kb_id=default_kb, category_id=None, content_type="long_text",
            status="completed", uploaded_by=admin.id, version_no=1,
        ))
        await db.commit()
        parent_id = parent.id

    r = await admin_client.get(
        "/api/v1/knowledge/documents",
        params={"kb_id": default_kb, "category_id": parent_id, "page_size": 100},
    )
    assert r.status_code == 200
    names = {i["original_name"] for i in r.json()["items"]}
    assert {"食堂文档0.txt", "食堂文档1.txt", "食堂文档2.txt"} <= names
    assert "车辆文档.txt" not in names  # 其他分类不被纳入
    # 批次F 验收：分类名拼父-子完整路径（只看子分类名满屏"制度"分不清主题）
    cat_names = {i["category_name"] for i in r.json()["items"]}
    assert "食堂-制度" in cat_names
    assert "食堂-表单" in cat_names

    # 按形态子分类筛选 → 只出该形态
    r2 = await admin_client.get(
        "/api/v1/knowledge/documents",
        params={"kb_id": default_kb, "category_id": kids["表单"].id, "page_size": 100},
    )
    names2 = {i["original_name"] for i in r2.json()["items"]}
    assert "食堂文档2.txt" in names2
    assert "食堂文档0.txt" not in names2


class TestDeleteCategoryGuard:
    """批次F 验收：删除分类护栏（有文档拒绝）+ 无文档级联删除子分类"""

    async def _seed(self, db, default_kb, with_doc: bool):
        from app.models.user import User

        admin = (await db.execute(
            select(User).where(User.role == "admin").limit(1)
        )).scalar_one()
        parent = KnowledgeCategory(name="删除测试", kb_id=default_kb)
        db.add(parent)
        await db.flush()
        kids = []
        for form in ("制度", "流程", "表单", "FAQ"):
            k = KnowledgeCategory(name=form, kb_id=default_kb, parent_id=parent.id)
            db.add(k)
            kids.append(k)
        await db.flush()
        if with_doc:
            db.add(Document(
                id="deldoc1", filename="deldoc1.txt", original_name="删除测试文档.txt",
                file_type="txt", file_size=10, storage_path="deldoc1.txt",
                kb_id=default_kb, category_id=kids[0].id, content_type="long_text",
                status="completed", uploaded_by=admin.id, version_no=1,
            ))
        await db.commit()
        return parent.id, [k.id for k in kids]

    @pytest.mark.asyncio
    async def test_delete_with_docs_rejected(self, admin_client, default_kb):
        """分类下有文档 → 400 中文护栏提示，分类未被删除"""
        async with async_session_factory() as db:
            parent_id, _ = await self._seed(db, default_kb, with_doc=True)
        r = await admin_client.delete(f"/api/v1/knowledge/categories/{parent_id}")
        assert r.status_code == 400
        assert "1 篇文档" in r.json()["detail"]
        async with async_session_factory() as db:
            still = (await db.execute(
                select(KnowledgeCategory).where(KnowledgeCategory.id == parent_id)
            )).scalar_one_or_none()
            assert still is not None

    @pytest.mark.asyncio
    async def test_delete_empty_cascades_children(self, admin_client, default_kb):
        """无文档 → 父分类与全部子分类级联删除（此前子分类成孤儿悬顶层）"""
        async with async_session_factory() as db:
            parent_id, kid_ids = await self._seed(db, default_kb, with_doc=False)
        r = await admin_client.delete(f"/api/v1/knowledge/categories/{parent_id}")
        assert r.status_code == 200
        async with async_session_factory() as db:
            parent = (await db.execute(
                select(KnowledgeCategory).where(KnowledgeCategory.id == parent_id)
            )).scalar_one_or_none()
            assert parent is None
            kids = (await db.execute(
                select(KnowledgeCategory).where(KnowledgeCategory.id.in_(kid_ids))
            )).scalars().all()
            assert kids == []  # 子分类一并删除

    @pytest.mark.asyncio
    async def test_delete_leaf_with_doc_rejected(self, admin_client, default_kb):
        """子分类下有文档、删其父分类 → 同样拒绝（护栏覆盖子孙分类）"""
        async with async_session_factory() as db:
            parent_id, kid_ids = await self._seed(db, default_kb, with_doc=True)
        # 删父分类（文档挂在子分类下）→ 拒绝
        r = await admin_client.delete(f"/api/v1/knowledge/categories/{parent_id}")
        assert r.status_code == 400
