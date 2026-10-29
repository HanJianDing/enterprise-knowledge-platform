"""系统配置服务 - 欢迎页快捷问题读写与启动种子"""
import json

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import SystemSetting

QUICK_QUESTIONS_KEY = "chat_quick_questions"

# 欢迎页默认推荐问题（管理端可改；与前端静态兜底列表保持一致）。
# 企业知识库改造阶段2：从客服口径换成企业通用口径。
DEFAULT_QUICK_QUESTIONS = [
    "年假怎么申请？",
    "报销流程是什么？",
    "入职需要准备哪些材料？",
    "公司考勤制度是怎样的？",
    "如何连接公司WiFi？",
    "IT报障找谁？",
]

# 客服口径时代（阶段2之前）的默认值：库里存的还是这两批旧值说明管理员从未改过，
# 启动时自动升级为企业口径新默认 6 条（管理员改过则不动）
LEGACY_DEFAULT_QUICK_QUESTIONS_V2 = [
    "我身高175体重70，男款外套选什么码？",
    "退换货流程是什么？",
    "有什么不辣的小零食推荐？",
    "物流一般几天能到？",
    "有XL码的衣服吗？",
    "怎么转人工客服？",
]
LEGACY_DEFAULT_QUICK_QUESTIONS_V1 = [
    "这件T恤有XL码吗？",
    "退换货政策是什么？",
    "我身高175体重70，穿什么尺码？",
    "物流配送要多久？",
]


async def seed_default_settings(db: AsyncSession) -> None:
    """启动种子：快捷问题键不存在则写入默认值。

    先查后插 + flush 异常兜底（PostgreSQL 下并发启动时可能主键冲突，
    回滚后跳过即可；种子在独立会话中执行，不影响其他写入）。
    """
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == QUICK_QUESTIONS_KEY)
    )
    row = result.scalar_one_or_none()
    if not row:
        db.add(SystemSetting(
            key=QUICK_QUESTIONS_KEY,
            value=json.dumps(DEFAULT_QUICK_QUESTIONS, ensure_ascii=False),
        ))
        try:
            await db.flush()
        except Exception:
            await db.rollback()
            logger.info(f"配置种子并发冲突，已跳过: {QUICK_QUESTIONS_KEY}")
        return

    # 自动升级：库里还是客服口径时代的默认值 → 换为企业口径新默认；
    # 管理员在后台改过的值保持不动
    for legacy in (LEGACY_DEFAULT_QUICK_QUESTIONS_V2, LEGACY_DEFAULT_QUICK_QUESTIONS_V1):
        if row.value == json.dumps(legacy, ensure_ascii=False):
            row.value = json.dumps(DEFAULT_QUICK_QUESTIONS, ensure_ascii=False)
            await db.flush()
            logger.info("快捷问题已从客服口径默认值自动升级为企业口径新默认 6 条")
            break


async def get_quick_questions(db: AsyncSession) -> list[str]:
    """读欢迎页快捷问题。无值/JSON 损坏时回退默认列表（fail-open：欢迎页永远有内容）。"""
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == QUICK_QUESTIONS_KEY)
    )
    row = result.scalar_one_or_none()
    if not row:
        return list(DEFAULT_QUICK_QUESTIONS)
    try:
        questions = json.loads(row.value)
        if isinstance(questions, list) and all(
            isinstance(q, str) and q.strip() for q in questions
        ):
            return [q.strip() for q in questions]
    except (json.JSONDecodeError, TypeError):
        logger.warning("快捷问题配置损坏，回退默认列表")
    return list(DEFAULT_QUICK_QUESTIONS)


async def set_quick_questions(db: AsyncSession, questions: list[str]) -> None:
    """写欢迎页快捷问题（管理端调用，即时生效）。"""
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == QUICK_QUESTIONS_KEY)
    )
    row = result.scalar_one_or_none()
    value = json.dumps([q.strip() for q in questions], ensure_ascii=False)
    if row:
        row.value = value
    else:
        db.add(SystemSetting(key=QUICK_QUESTIONS_KEY, value=value))
    await db.flush()
