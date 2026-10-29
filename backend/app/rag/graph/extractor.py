"""实体关系抽取器（验收批次2）——借鉴 RAGFlow light 方案 + 窄场景收紧

- 批打包：chunk 按 2000 字重打包成"抽取批"（LLM 上下文可控）
- LLM 抽取（qwen-turbo，非流式）→ JSON 解析（剥围栏）
- 过滤：实体类型白名单 + 关系受控词表映射（映射不了丢弃）+
  质量过滤（描述空/两端同名 丢弃）
"""
import json
import logging

from app.rag.graph.prompts import ENTITY_EXTRACT_PROMPT, ENTITY_TYPES, RELATION_VOCAB

logger = logging.getLogger(__name__)

_BATCH_CHARS = 2000

# 噪音实体停用集（批次2-2 收紧）：LLM 常把泛称抽成实体，
# 这些名字没有图检索价值（精确匹配，只拦名字本身，不误伤含这些字的复合名）。
# 中文制度文本高频示例人名（"张三""李四"类举例人物）一并拦截——
# 它们入图会让"张三和王五是什么关系"误介入，产出幻觉级答案；
# 真实企业制度若确有同名员工，由管理员在提示词/词表层单独调整
_NOISE_NAMES = {
    "公司", "企业", "员工", "部门", "制度", "文档", "文件",
    "本文", "本制度", "本公司", "会议", "公司员工", "全体员工",
    "张三", "李四", "王五", "赵六", "马六", "赵七", "王小明", "小李",
}


def _normalize_relation(relation_text: str) -> str | None:
    """关系文本映射到受控词表（8 点改进 2）；映射不了返回 None 丢弃"""
    text = (relation_text or "").strip()
    for canon, aliases in RELATION_VOCAB.items():
        if text in aliases or text == canon:
            return canon
    # 宽松匹配：文本包含词表别名也算命中（如"上级是"→上级）
    for canon, aliases in RELATION_VOCAB.items():
        for alias in aliases:
            if alias and alias in text:
                return canon
    return None


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def pack_batches(chunks: list[str], batch_chars: int = _BATCH_CHARS) -> list[str]:
    """chunk 列表按字符预算打包成抽取批（≤2000 字/批）"""
    batches: list[str] = []
    acc = ""
    for chunk in chunks:
        if acc and len(acc) + len(chunk) + 1 > batch_chars:
            batches.append(acc)
            acc = chunk
        else:
            acc = acc + ("\n" if acc else "") + chunk
    if acc:
        batches.append(acc)
    return batches


def parse_extraction(text: str) -> dict:
    """解析 LLM 抽取输出 → 过滤后的 {entities, relations}"""
    try:
        data = json.loads(_strip_code_fence(text))
        if not isinstance(data, dict):  # 裸数组等畸形输出兜底
            return {"entities": [], "relations": []}
    except (json.JSONDecodeError, TypeError):
        return {"entities": [], "relations": []}

    allowed_types = set(ENTITY_TYPES)
    entities = []
    for e in data.get("entities", []):
        name = (e.get("name") or "").strip()
        etype = (e.get("type") or "").strip().lower()
        desc = (e.get("description") or "").strip()
        if not name or etype not in allowed_types:
            continue
        # 批次2-2 收紧：单字名（"王"）与泛称停用词（"公司"）无图检索价值，丢弃
        if len(name) < 2 or name in _NOISE_NAMES:
            continue
        if desc and desc not in entities:  # 质量过滤：描述为空的实体不留
            entities.append({
                "name": name,
                "type": etype,
                "description": desc,
            })

    relations = []
    for r in data.get("relations", []):
        src = (r.get("source") or "").strip()
        dst = (r.get("target") or "").strip()
        relation = _normalize_relation(r.get("relation") or "")
        if not src or not dst or src == dst or relation is None:
            continue  # 质量过滤：两端同名/关系不在词表 丢弃
        try:
            strength = int(r.get("strength") or 4)
        except (TypeError, ValueError):
            strength = 4
        relations.append({
            "source": src,
            "target": dst,
            "relation": relation,
            "strength": max(1, min(10, strength)),
        })
    return {"entities": entities, "relations": relations}


def build_extract_prompt(batch_text: str) -> str:
    return ENTITY_EXTRACT_PROMPT.format(
        entity_types=", ".join(ENTITY_TYPES),
        relation_vocab=json.dumps(RELATION_VOCAB, ensure_ascii=False),
        text=batch_text,
    )
