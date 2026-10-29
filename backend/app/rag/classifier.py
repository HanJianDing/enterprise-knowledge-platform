"""问题意图识别 + 分类 Agent — 一次 LLM 调用同时完成意图澄清和分类判断

企业知识库改造阶段2：从电商客服口径改为企业知识库口径；
移除 needs_human/upset（转人工/情绪判断随客服功能剥离）。
"""
import json
import re
from loguru import logger
from app.config import settings
from app.rag.llm import get_llm


CLASSIFICATION_PROMPT = """你是企业知识库的意图识别与分类助手。

## 任务（需同时完成三项）

### 1. 意图澄清
员工可能使用口语、简称、错别字、模糊指代来表达。你需要理解真实意图，改写为一个关键词丰富、适合向量检索的检索语句。
改写原则：
- "年假" → "年假政策 带薪年假 申请条件"
- "报销" → "报销流程 报销标准 发票要求"
- "电脑坏了找谁" → "IT报障 电脑维修 技术支持"
- "新来的" → "入职材料 入职手续 新人须知"
- 如果员工已经表达清晰，直接复用原问题即可

### 2. 分类判断
根据改写后的意图，从可用分类中选择最匹配的一个。

## 分类层级选择规则

根据员工问题的特征，决定选择父分类还是子分类：

### 1. 对比/多主题 → 选父分类
如果员工问题满足以下**任一**条件，选择这些主题所属的**公共父分类**：
- 出现"对比""哪个好""有什么区别""比较""有哪些"等对比/枚举类关键词
- 同时提到**两个及以上**不同主题
- 例："年假和病假有什么区别" → 选父分类"休假制度"

### 2. 单一主题 → 选子分类
如果员工问题**只涉及一个**明确的主题，选择该主题对应的**叶子分类**：
- 例："年假怎么申请" → 选子分类"年假政策"
- 例："差旅住宿标准多少" → 选子分类"差旅报销"

### 3. 无具体主题 → 选父分类
如果员工问题**没有提到任何**具体主题名，选择**父分类**进行宽泛搜索：
- 例："休假都有什么规定" → 选父分类"休假制度"
- 例："报销怎么弄" → 选父分类"报销"

### 3. 复杂度判断（产品化 B1：复杂问题走多路检索编排）
根据问题特征判断复杂度，取以下六值之一：
- "simple"：普通单一问题（绝大多数情况）
- "compare"：对比/区别类——"对比""区别""有何不同""哪个好""vs"，或同时涉及两个主题
- "calculate"：计算类——需要算术运算才能回答（"还剩""合计""总共""扣除后"等）
- "multi"：多问题类——一句话里包含多个独立问题
- "relation"：关系/汇报链类（验收批次2）——问人物/部门之间的**关系、上下级、汇报链**（如"张三和王五是什么关系""谁的上级是X""郑浩向谁汇报"），走知识图谱检索
- "approval"：审批链规则类（批次2-4 规则腿）——问"报销/采购/请假"的**金额或天数对应谁审批/备案**，且问句含具体数字（如"报销 8000 元谁审批""请假 5 天找谁批"），走规则引擎确定性匹配；没有数字的泛问（"报销怎么审批"）不算
拿不准就选 "simple"（宁可走普通流程，不误触发编排）。

## 通用规则
- 如果问题与所有分类都不相关，category_id 为 null，category_name 为空字符串
- 只输出 JSON，不要附带任何 Markdown 代码块标记或解释文字

## 可用分类
{categories_text}

## 用户问题
{question}

## 输出格式（严格遵守，不要包含 ```json 包裹）
{{"clarified_question": "改写后的检索语句", "category_id": "分类ID或null", "category_name": "分类名称或空字符串", "complexity": "simple/compare/calculate/multi/relation/approval"}}"""


_COMPLEXITY_VALUES = {"simple", "compare", "calculate", "multi", "relation", "approval"}


def _parse_complexity(result: dict) -> str:
    """解析复杂度字段；非法/缺失一律回落 simple（宁可走普通流程，不误触发编排）"""
    value = str(result.get("complexity", "simple") or "simple").strip().lower()
    return value if value in _COMPLEXITY_VALUES else "simple"


def classify_question(
    question: str,
    categories: list[dict],
    history_hint: str | None = None,
) -> dict | None:
    """
    意图澄清 + 分类：一次 LLM 调用同时完成。

    Args:
        question: 员工原始提问（可能口语化/模糊）
        categories: 分类列表，每项含 {id, name, path}（path 如 "休假制度 > 年假政策"）
        history_hint: 可选的历史提示（预留参数，当前未使用）

    Returns:
        {
            "clarified_question": str,   # 改写后的问题（用于检索）
            "category_id": str | None,   # 匹配的分类ID
            "category_name": str,        # 分类名称
            "complexity": str,           # simple/compare/calculate/multi（产品化 B1）
        }
        或 None（解析失败/无匹配）
    """
    if not categories:
        logger.info("分类列表为空，跳过分类")
        return None

    # 构建分类文本（展示层级路径）
    cat_lines = []
    id_to_name = {}
    for cat in categories:
        line = f"- ID: {cat['id']} | 名称: {cat.get('path', cat['name'])}"
        cat_lines.append(line)
        id_to_name[cat["id"]] = cat["name"]

    categories_text = "\n".join(cat_lines)

    prompt = CLASSIFICATION_PROMPT.format(
        categories_text=categories_text,
        question=question,
    )

    try:
        # 延迟优化 D：分类任务简单，用轻量快速模型（settings.classifier_model，
        # 默认 qwen-turbo），生成回答仍用 llm_model
        llm = get_llm(streaming=False, model=settings.classifier_model)
        response = llm.invoke(prompt)
        raw_output = response.content if hasattr(response, "content") else str(response)
        logger.info(f"意图+分类 Agent 输出: {raw_output[:300]}")

        # 提取 JSON（兼容 LLM 偶尔带 markdown 包裹）
        json_match = re.search(r'\{[^{}]*"clarified_question"[^{}]*\}', raw_output, re.DOTALL)
        if not json_match:
            # 回退：尝试匹配任意含 category_id 的 JSON
            json_match = re.search(r'\{[^{}]*"category_id"[^{}]*\}', raw_output, re.DOTALL)
        if not json_match:
            # 最后兜底：匹配任意 JSON
            json_match = re.search(r'\{[^{}]*\}', raw_output, re.DOTALL)

        if not json_match:
            logger.warning(f"无法从输出中提取 JSON: {raw_output[:300]}")
            return None

        result = json.loads(json_match.group(0))

        clarified = (result.get("clarified_question") or "").strip()
        category_id = result.get("category_id")
        category_name = (result.get("category_name") or "").strip()

        # 如果没有 clarified_question，用原始问题兜底
        if not clarified:
            clarified = question

        # 校验 category_id
        if category_id is None or str(category_id).lower() in ("null", "none", ""):
            logger.info(f"问题未匹配到分类，仅做意图澄清: {question[:50]}... → {clarified[:50]}...")
            return {
                "clarified_question": clarified,
                "category_id": None,
                "category_name": "",
                "complexity": _parse_complexity(result),
            }

        if category_id not in id_to_name:
            logger.warning(f"LLM 返回了未知 category_id: {category_id}")
            return None

        # 优先用已知名称
        resolved_name = id_to_name.get(category_id, category_name)
        logger.info(
            f"意图+分类: [{resolved_name}] ← {question[:50]}... → clarified: {clarified[:60]}..."
        )
        return {
            "clarified_question": clarified,
            "category_id": category_id,
            "category_name": resolved_name,
            "complexity": _parse_complexity(result),
        }

    except json.JSONDecodeError as e:
        logger.warning(f"JSON 解析失败: {e}, 输出: {raw_output[:300]}")
        return None
    except Exception as e:
        logger.error(f"意图+分类 Agent 异常: {e}")
        return None
