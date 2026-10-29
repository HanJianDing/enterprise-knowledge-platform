"""演示环境种子脚本：虚构企业知识库一键生成（产品化·体验数据）

演示模式（默认）与真实环境完全隔离：ragdb_demo + chroma_demo + uploads_demo。
真实模式（--target real，验收批次C）把同样内容同步进真实库（幂等，不动原有数据）。

生成内容（全部为虚构数据，与任何真实企业无关）：
- 6 部门 + 6 部门私有库 + 1 全员公共库 + 1 经营管理库（mgmt01 管理层 + 财务部可见）
- 两层分类树（主题父分类 + 制度/流程/表单/FAQ 形态子分类）
- 9 个账号（6 部门员工 + 1 审计员 + 1 管理员 + 1 管理层 mgmt01）
- 文档：读取 D:\\文档数据（跳过 体验上传\\ 子目录——留给用户手动上传）

用法（backend 目录下，需真实 .env 的 DASHSCOPE_API_KEY）：
    python scripts/seed_demo.py                  # 演示环境全量：建库+组织+上传
    python scripts/seed_demo.py --org-only       # 只建组织（不上传文档）
    python scripts/seed_demo.py --target real    # 真实环境同步（幂等）
"""
import argparse
import asyncio
import io
import os
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT = BACKEND_DIR.parent

# 先解析 --target（在环境隔离之前决定指向哪个库）
_TARGET_PARSER = argparse.ArgumentParser(add_help=False)
_TARGET_PARSER.add_argument("--target", choices=["demo", "real"], default="demo")
_TARGET_PARSER.add_argument("--org-only", action="store_true")
_target_args, _ = _TARGET_PARSER.parse_known_args()
TARGET_REAL = _target_args.target == "real"

# ── 环境设置（必须在 import app 之前） ──
def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env(ROOT / ".env")  # 真实密钥（DASHSCOPE_API_KEY 与真实库地址）
import urllib.parse

_real_url = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:CHANGE_ME_DATABASE_PASSWORD@localhost:5432/ragdb"
)
if not TARGET_REAL:
    # 演示模式：隔离变量指向演示库/目录；.env.demo 只在演示模式加载——
    # 质检修复：真实模式不得加载演示专用配置（未来新增演示键会漏进真实运行，
    # 最危险的是 INITIAL_ADMIN_PASSWORD 在真实库无 admin 时被用于创建管理员）
    _load_env(ROOT / ".env.demo")
    _parsed = urllib.parse.urlparse(_real_url)
    DEMO_DB_URL = _parsed._replace(path="/ragdb_demo").geturl()
    os.environ["DATABASE_URL"] = DEMO_DB_URL
    os.environ["CHROMA_PERSIST_DIR"] = str(BACKEND_DIR / "chroma_demo")
    os.environ["UPLOAD_DIR"] = str(BACKEND_DIR / "uploads_demo")
    # 直接赋值而非 setdefault：真实 .env 若有 INITIAL_ADMIN_PASSWORD，
    # setdefault 会让真实值生效——演示库 admin 将用真实密码，隔离被打破
    # （质检必修项：演示库必须只用演示密码 CHANGE_ME_DEMO_PASSWORD）
    os.environ["INITIAL_ADMIN_PASSWORD"] = os.environ["DEMO_ADMIN_PASSWORD"]

sys.path.insert(0, str(BACKEND_DIR))

DOCS_DIR = Path("D:/文档数据")
# 体验上传目录不在 FOLDER_KB_MAP 中，天然跳过（留给用户手动上传）

# 目录 → (知识库名, 内容类型)
FOLDER_KB_MAP = {
    "人事部": ("人事制度库", "long_text"),
    "财务部": ("财务制度库", "long_text"),
    "行政部": ("行政制度库", "long_text"),
    "IT部": ("IT支持库", "long_text"),
    "市场部": ("市场资料库", "long_text"),
    "研发部": ("研发规范库", "long_text"),
    "公共": ("公司公共库", "long_text"),
}

DEFAULT_PASSWORD = os.environ["DEMO_EMPLOYEE_PASSWORD"]

# ── 两层分类推导（验收批次B：主题父分类 + 形态子分类） ──
# 主题从文档文件名剥离已知后缀推导；形态按扩展名与关键词判定
_THEME_SUFFIXES = [
    "管理制度", "操作流程", "登记表", "常见问题", "FAQ", "管理办法",
    "管理规范", "管理流程", "管理指南", "对照表", "速查", "清单",
    "管理", "制度", "流程", "规范", "标准", "指南",
]


# 验收批次0（语料扩充）：文件名 stem → 主题的显式映射（后缀剥离规则推不出的新主题）
_THEME_OVERRIDES = {
    "员工手册": "员工手册",
    "管理制度汇编": "制度汇编",
    "2025年度经营分析报告": "年度报告",
    "新员工入职培训教材": "培训教材",
    "信息安全培训教材": "培训教材",
    "2025年度会议纪要汇编": "会议纪要",
    "制度全集逐条解读": "制度解读",
    "产品与技术白皮书": "产品白皮书",
    "组织架构与汇报关系制度": "组织架构",
    "审批权限与流程制度": "审批权限",
}


def theme_of(filename: str) -> str:
    name = Path(filename).stem
    if name in _THEME_OVERRIDES:
        return _THEME_OVERRIDES[name]
    for _ in range(5):
        changed = False
        for suf in _THEME_SUFFIXES:
            if name.endswith(suf) and len(name) > len(suf):
                name = name[: -len(suf)]
                changed = True
        if not changed:
            break
    if "差旅安全" in name:
        # 验收批次E：先判"差旅安全"——旧顺序只判"差旅"，把《差旅安全》系列
        # 误归入"差旅"主题（质检建议级）
        name = "差旅安全"
    elif "差旅" in name:
        name = "差旅"
    if "请假类型" in name:
        name = "请假"
    if name.endswith("通知") or name.endswith("公告"):
        # 验收批次0：各类短通知/公告统一归"通知公告"主题（one 场景语料）
        name = "通知公告"
    return name.strip() or "综合"


def form_of(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in (".csv", ".xlsx"):
        return "表单"
    if "FAQ" in filename or "常见问题" in filename:
        return "FAQ"
    if "流程" in filename:
        return "流程"
    return "制度"


async def ensure_demo_db() -> None:
    """创建 ragdb_demo（不存在时）；用维护库连接执行 CREATE DATABASE"""
    import asyncpg

    parsed = urllib.parse.urlparse(_real_url)
    conn = await asyncpg.connect(
        host=parsed.hostname, port=parsed.port or 5432,
        user=parsed.username, password=parsed.password,
        database="postgres",
    )
    exists = await conn.fetchval(
        "SELECT 1 FROM pg_database WHERE datname = 'ragdb_demo'"
    )
    if not exists:
        await conn.execute('CREATE DATABASE ragdb_demo')
        print("已创建演示数据库 ragdb_demo")
    await conn.close()


def run_migrations() -> None:
    """对演示库执行 alembic 迁移（子进程，环境变量已指向演示库）"""
    import subprocess

    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"迁移失败: {result.stderr}")
        sys.exit(1)
    print("数据库迁移完成")


async def build_org() -> None:
    """建部门/知识库/分类/授权/账号（全部虚构数据）"""
    from sqlalchemy import select

    from app.infrastructure.database import async_session_factory
    from app.middleware.auth import hash_password
    from app.models.document import KnowledgeCategory
    from app.models.knowledge_base import Department, KnowledgeBase, KbMember, UserDepartment
    from app.models.user import User
    from app.main import _ensure_admin

    await _ensure_admin()

    # created_by 是 users.id 外键：取演示库的 admin 用户 id；
    async with async_session_factory() as db:
        admin_row = (await db.execute(
            select(User).where(User.role == "admin").limit(1)
        )).scalar_one()
        if not TARGET_REAL:
            # 演示 admin 密码强制同步为 CHANGE_ME_DEMO_PASSWORD（幂等重跑也会修正——
            # 修复此前 setdefault 被真实密码污染过的演示库）
            # 真实模式绝不碰真实管理员密码
            admin_row.hashed_password = hash_password("CHANGE_ME_DEMO_PASSWORD")
            admin_row.must_change_password = False
            await db.commit()
        admin_id = admin_row.id
        # 验收批次0：不再"已有分类即跳过"——新语料引入新主题时需要补建缺失分类。
        # 下方部门/库/分类/账号创建全部按名称幂等（验收批次C），重复运行安全
        await db.commit()

    # 两层分类树（验收批次B）：主题与形态推导函数已提升到模块级（theme_of/form_of）
    # 预扫文档目录：得到 (目录→主题集合)
    folder_themes: dict[str, set[str]] = {}
    for folder, (kb_name, _ct) in FOLDER_KB_MAP.items():
        folder_path = DOCS_DIR / folder
        themes = set()
        if folder_path.exists():
            for f in folder_path.iterdir():
                if f.is_file() and f.suffix.lower() in (".txt", ".md", ".csv", ".xlsx", ".docx"):
                    themes.add(theme_of(f.name))
        folder_themes[folder] = themes

    dept_kbs = [
        ("人事部", "人事制度库", "考勤/假期/招聘/绩效相关制度与流程"),
        ("财务部", "财务制度库", "报销/差旅/采购/预算相关制度与标准"),
        ("行政部", "行政制度库", "办公用品/会议室/车辆/安全等行政规范"),
        ("IT部", "IT支持库", "账号/网络/设备报障与软件使用指南"),
        ("市场部", "市场资料库", "品牌规范/活动流程/物料标准"),
        ("研发部", "研发规范库", "代码规范/发布流程/项目管理制度"),
    ]
    categories = ["制度", "流程", "表单", "FAQ"]  # 形态子分类名（验收批次B：挂在主题父分类下）

    async with async_session_factory() as db:
        # 按名称幂等：真实环境可能已有同名部门/库（验收批次C），复用而非新建
        existing_depts = {d.name: d for d in (await db.execute(select(Department))).scalars().all()}
        existing_kbs = {k.name: k for k in (await db.execute(select(KnowledgeBase))).scalars().all()}
        existing_users = {u.username for u in (await db.execute(select(User))).scalars().all()}

        # 部门
        dept_rows = {}
        for name, _, _ in dept_kbs:
            row = existing_depts.get(name)
            if row is None:
                row = Department(name=name)
                db.add(row)
                await db.flush()
            dept_rows[name] = row

        # 私有库 + 授权本部门（可读可传可管；库已存在时不重复建授权）
        kb_rows = {}
        for name, kb_name, desc in dept_kbs:
            kb = existing_kbs.get(kb_name)
            if kb is None:
                kb = KnowledgeBase(name=kb_name, description=desc, is_public=False,
                                   created_by=admin_id)
                db.add(kb)
                await db.flush()
                db.add(KbMember(kb_id=kb.id, subject_type="dept",
                                subject_id=dept_rows[name].id,
                                can_read=True, can_upload=True, can_manage=True))
            kb_rows[kb_name] = kb

        # 公共库（全员可读）
        pub = existing_kbs.get("公司公共库")
        if pub is None:
            pub = KnowledgeBase(name="公司公共库", description="全员可见的公司级制度与通知",
                                is_public=True, created_by=admin_id)
            db.add(pub)
            await db.flush()
        kb_rows["公司公共库"] = pub

        # 经营管理库（验收批次1·8j 管理层分级：敏感经营数据单独成库，
        # 只授权管理层账号与财务部——员工按部门权限看不到）
        mgmt_kb = existing_kbs.get("经营管理库")
        if mgmt_kb is None:
            mgmt_kb = KnowledgeBase(name="经营管理库", description="经营数据（年报/预算），仅管理层与财务部可见",
                                    is_public=False, created_by=admin_id)
            db.add(mgmt_kb)
            await db.flush()

        # 两层分类树（验收批次B）：主题父分类 + 形态子分类，按名称幂等创建
        folder_to_kb = {folder: kb_name for folder, (kb_name, _ct) in FOLDER_KB_MAP.items()}
        for folder, themes in folder_themes.items():
            kb = kb_rows.get(folder_to_kb.get(folder, ""))
            if kb is None:
                continue
            parent_rows = {
                c.name: c for c in (await db.execute(select(KnowledgeCategory).where(
                    KnowledgeCategory.kb_id == kb.id,
                    KnowledgeCategory.parent_id.is_(None),
                ))).scalars().all()
            }
            for theme in sorted(themes):
                parent = parent_rows.get(theme)
                if parent is None:
                    parent = KnowledgeCategory(name=theme, kb_id=kb.id, parent_id=None)
                    db.add(parent)
                    await db.flush()
                    parent_rows[theme] = parent
                child_names = {
                    c.name for c in (await db.execute(select(KnowledgeCategory).where(
                        KnowledgeCategory.parent_id == parent.id,
                    ))).scalars().all()
                }
                for form in categories:
                    if form not in child_names:
                        db.add(KnowledgeCategory(name=form, kb_id=kb.id, parent_id=parent.id))

        # 账号：6 部门员工 + 1 审计员；it01 保留 must_change_password 演示首登改密。
        # 真实模式同样创建（用户已拍板：账号同步进真实库）；已存在用户名跳过
        accounts = [
            ("hr01", "人事部"), ("fin01", "财务部"), ("adm01", "行政部"),
            ("it01", "IT部"), ("mkt01", "市场部"), ("rd01", "研发部"),
        ]
        for username, dept_name in accounts:
            if username in existing_users:
                continue
            user = User(
                username=username, email=f"{username}@demo.local",
                hashed_password=hash_password(DEFAULT_PASSWORD), role="user",
                must_change_password=(username == "it01"),
            )
            db.add(user)
            await db.flush()
            db.add(UserDepartment(user_id=user.id, department_id=dept_rows[dept_name].id))

        if "auditor01" not in existing_users:
            db.add(User(
                username="auditor01", email="auditor01@demo.local",
                hashed_password=hash_password(DEFAULT_PASSWORD), role="auditor",
                must_change_password=False,
            ))

        # 管理层账号 mgmt01（验收批次1·8j）：角色仍为 user（权限模型不扩角色），
        # 以"个人授权"接入经营管理库——演示"管理员管系统/管理层看数据/员工按部门"
        if "mgmt01" not in existing_users:
            mgmt_user = User(
                username="mgmt01", email="mgmt01@demo.local",
                hashed_password=hash_password(DEFAULT_PASSWORD), role="user",
                must_change_password=False,
            )
            db.add(mgmt_user)
            await db.flush()
        else:
            mgmt_user = (await db.execute(
                select(User).where(User.username == "mgmt01")
            )).scalar_one()
        # 经营管理库授权：mgmt01 个人（读+传+管）+ 财务部部门（读）
        if not (await db.execute(
            select(KbMember).where(
                KbMember.kb_id == mgmt_kb.id, KbMember.subject_type == "user",
                KbMember.subject_id == mgmt_user.id,
            )
        )).scalar_one_or_none():
            db.add(KbMember(kb_id=mgmt_kb.id, subject_type="user", subject_id=mgmt_user.id,
                            can_read=True, can_upload=True, can_manage=True))
        if not (await db.execute(
            select(KbMember).where(
                KbMember.kb_id == mgmt_kb.id, KbMember.subject_type == "dept",
                KbMember.subject_id == dept_rows["财务部"].id,
            )
        )).scalar_one_or_none():
            db.add(KbMember(kb_id=mgmt_kb.id, subject_type="dept", subject_id=dept_rows["财务部"].id,
                            can_read=True, can_upload=True, can_manage=False))

        await db.commit()
        print("组织结构已创建：6 部门 + 8 知识库 + 两层分类树（主题父+形态子） + 9 账号")


async def upload_docs() -> int:
    """批量上传 D:\\文档数据（跳过 体验上传 子目录）；返回成功数"""
    from sqlalchemy import select

    from app.infrastructure.database import async_session_factory
    from app.models.document import Document, KnowledgeCategory
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User
    from app.services import document_service
    from starlette.datastructures import UploadFile

    if not DOCS_DIR.exists():
        print(f"文档目录不存在: {DOCS_DIR}")
        return 0

    # 收集文件
    jobs = []
    for folder, (kb_name, content_type) in FOLDER_KB_MAP.items():
        folder_path = DOCS_DIR / folder
        if not folder_path.exists():
            continue
        for f in sorted(folder_path.iterdir()):
            if f.is_file():
                jobs.append((f, kb_name, content_type))

    async with async_session_factory() as db:
        admin = (await db.execute(
            select(User).where(User.role == "admin").limit(1)
        )).scalar_one()
        kb_name_to_row = {}
        for kb in (await db.execute(select(KnowledgeBase))).scalars().all():
            kb_name_to_row[kb.name] = kb
        cat_rows = (await db.execute(select(KnowledgeCategory))).scalars().all()
        # 幂等：已上传过的文件跳过（重跑种子不产生重复文档）
        existing_names = {
            name for (name,) in (await db.execute(select(Document.original_name))).all()
        }

        ok = failed = skipped = 0
        for i, (file_path, kb_name, content_type) in enumerate(jobs, 1):
            if file_path.name in existing_names:
                skipped += 1
                continue
            kb = kb_name_to_row.get(kb_name)
            if kb is None:
                print(f"  [{i}/{len(jobs)}] 跳过（无库）: {file_path.name}")
                continue
            # 内容类型按扩展名细化：csv/xlsx → table；含 Q:/问： 的 md → faq
            ct = content_type
            ext = file_path.suffix.lower()
            if ext in (".csv", ".xlsx"):
                ct = "table"
            elif ext in (".md", ".txt"):
                text = file_path.read_text(encoding="utf-8", errors="ignore")[:500]
                if "问：" in text or "Q:" in text:
                    ct = "faq"
            # 两层分类归属（验收批次B）：主题父分类 + 形态子分类
            theme = theme_of(file_path.name)
            form = form_of(file_path.name)
            parent = next((c for c in cat_rows
                           if c.kb_id == kb.id and c.parent_id is None and c.name == theme), None)
            cat = next((c for c in cat_rows
                        if parent is not None and c.parent_id == parent.id and c.name == form), None) if parent else None
            if cat is None:
                # 质检修复：旧库幂等重跑没有两层分类树时，文档会静默挂 category=None——
                # 至少留一条警告，提示需要重建演示库
                print(f"  ⚠ {file_path.name}：未找到主题[{theme}]分类，文档将不挂分类"
                      f"（若为旧演示库，请 drop ragdb_demo 后重跑种子）")
            try:
                data = file_path.read_bytes()
                uf = UploadFile(filename=file_path.name, file=io.BytesIO(data))
                resp = await document_service.upload_document(
                    db, admin, uf, kb.id, category_id=cat.id if cat else None,
                    content_type=ct,
                )
                # 等处理完成（后台任务 + 嵌入 API；逐份等待避免并发打爆限流）
                deadline = time.monotonic() + 300
                while document_service._in_flight_doc_ids and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                # 超时兜底校验：确认文档到达终态才算成功（防"等了 300 秒还没处理完"
                # 却被记成功；正常路径该检查是瞬时查询）
                doc_row = (await db.execute(
                    select(Document).where(Document.id == resp.id)
                )).scalar_one()
                if doc_row.status not in ("completed", "failed"):
                    failed += 1
                    print(f"  [{i}/{len(jobs)}] ✗ {file_path.name}: 处理超时（状态 {doc_row.status}）")
                    continue
                ok += 1
                print(f"  [{i}/{len(jobs)}] ✓ {file_path.name} → {kb_name}（{ct}）")
            except Exception as e:
                failed += 1
                print(f"  [{i}/{len(jobs)}] ✗ {file_path.name}: {e}")
        await db.commit()

    print(f"上传完成：成功 {ok}，失败 {failed}，跳过（已存在）{skipped}，共 {len(jobs)} 份")
    return ok


async def main(org_only: bool) -> None:
    print("=" * 60)
    if TARGET_REAL:
        print("真实环境同步（验收批次C，幂等——已存在的部门/库/账号/文档跳过）")
        # 质检修复：连接串掩码密码后再打印，绝不明文输出凭据
        _u = urllib.parse.urlparse(os.environ.get("DATABASE_URL", ""))
        _masked = _u._replace(netloc=f"{_u.username or '?'}:***@{_u.hostname}:{_u.port or 5432}").geturl()
        print(f"  数据库: {_masked}")
    else:
        print("演示环境种子脚本（虚构数据，与真实环境隔离）")
        print("  数据库: ragdb_demo")
    print(f"  文档目录: {DOCS_DIR}")
    print("=" * 60)
    if TARGET_REAL:
        run_migrations()  # 真实库先升到最新 schema（含两层分类约束迁移）
    else:
        await ensure_demo_db()
        run_migrations()
    await build_org()
    if not org_only:
        await upload_docs()
    # 批次2-2 env 标签迁移：存量无 env 节点打上本环境标签（幂等，无存量时为 0）。
    # 仅演示环境执行——存量数据全部产生于演示环境；真实环境启用时存量已带标签
    if not TARGET_REAL:
        from app.config import settings
        from app.rag.graph import neo4j_store

        if settings.graphrag_enabled:
            migrated = await neo4j_store.migrate_legacy_nodes()
            if migrated:
                print(f"图谱存量节点已迁移 env 标签: {migrated} 个")
    if TARGET_REAL:
        print("\n真实环境同步完成。真实环境启动仍用 python run.py（端口 8000）")
    else:
        print("\n种子完成。启动演示环境：项目根目录 python run_demo.py（端口 8010）")
    print(f"账号（密码均为 {DEFAULT_PASSWORD}）：")
    print("  hr01/fin01/adm01/it01/mkt01/rd01（六部门员工，it01 首登需改密）")
    print("  auditor01（审计员） / admin（管理员）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="演示环境种子脚本")
    parser.add_argument("--target", choices=["demo", "real"], default="demo",
                        help="demo=演示隔离环境（默认）；real=真实环境同步（幂等）")
    parser.add_argument("--org-only", action="store_true", help="只建组织与账号，不上传文档")
    parser.add_argument("--yes", action="store_true",
                        help="real 目标的安全确认（质检修复：误跑会向真实库迁移 schema + 写入数据）")
    args = parser.parse_args()
    if args.target == "real" and not args.yes:
        parser.error("--target real 会向真实库迁移 schema 并写入演示数据，需加 --yes 确认")
    asyncio.run(main(args.org_only))
