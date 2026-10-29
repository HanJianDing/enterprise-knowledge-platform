"""图谱查询链路（验收批次2）——问题 → 实体 → 路径 → 文本

KGSearch 等价实现（对照 RAGFlow search.py 思路，用真图遍历）：
1. 查询重写：LLM 从问题抽实体名（≤5；失败回退整句）
2. 实体链接（8 点改进 3 + 批次2-2 完整化）：
   ①确定性名字匹配（图节点名出现在问句，长名优先）
   ②别名消歧兜底：问句称呼（"王总"）→ 向量召回候选节点 → LLM 确认
     （"王总"→"王建国"）——对齐不到则跳过
3. 图谱查询：≥2 实体走 shortestPath 路径；1 实体走邻居
4. 组织文本：实体表 + 关系/路径文字，返回合成上下文
5. 介入条件（8 点改进 5）：实体链接成功且（有路径 或 ≥1 实体邻居 或
   社区命中）才返回图谱上下文；批次2-5 起无实体锚定的社区级问法
   （"研发团队有哪些人"）经 embedding+LLM 确认命中社区也可介入，
   否则 None——上层静默回退纯向量检索
"""
import asyncio
import json
import logging

from app.rag.graph import neo4j_store
from app.rag.graph.prompts import DISAMBIGUATE_PROMPT, QUERY_REWRITE_PROMPT

logger = logging.getLogger(__name__)

# 候选池阈值：图节点数超过该值时向量召回截断（小图全量喂 LLM 更准）
_FULL_CANDIDATE_LIMIT = 50

# 邻居意图关键词（批次2-2 补强）：问句含下属意图 → 只留下属方向的边；
# 含上级意图 → 只留上级方向；都不含 → 不过滤（全量邻居）
_SUBORDINATE_HINTS = ("下属", "下级", "部下", "管理哪些人", "带哪些人", "谁归我管")
_SUPERIOR_HINTS = ("上级", "领导", "向谁汇报", "汇报给谁", "归谁管", "老板", "上司")

# 聚合意图关键词（批次2-3）：问"团队/圈子/成员"走社区（连接密度圈，
# 与"下属"=直接汇报关系不同）
_COMMUNITY_HINTS = ("团队", "圈子", "成员", "有哪些人", "一共多少人", "多少人")


def _neighbor_intent(question: str) -> str | None:
    """问句意图 → 邻居过滤方向：'subordinates'/'superior'/None（不过滤）"""
    if any(k in question for k in _SUBORDINATE_HINTS):
        return "subordinates"
    if any(k in question for k in _SUPERIOR_HINTS):
        return "superior"
    return None


def _has_recursive_superior(question: str) -> bool:
    """递归上级问法检测（批次2-3）："的上级"出现 ≥2 次（"郑浩的上级的上级是谁"）
    或含"再上一级/两级/几级" → 走定向多跳链"""
    if question.count("的上级") >= 2:
        return True
    return any(k in question for k in ("再上一级", "两级", "几级", "上两级"))


def _superior_hops(question: str) -> int:
    """期望跳数：问句"的上级"重复次数（"的上级的上级"=2）；关键词变体
    （"再上一级/两级/几级"，质检#3）给兜底跳数。钳制 1~3"""
    n = question.count("的上级")
    if n < 2:
        if "两级" in question or "上两级" in question:
            n = 2
        elif "再上一级" in question:
            n = 2
        elif "几级" in question:
            n = 3
    return max(1, min(3, n))


def _has_community_intent(question: str) -> bool:
    """聚合意图检测：问团队/圈子/成员类问题走社区"""
    return any(k in question for k in _COMMUNITY_HINTS)


# 社区向量召回最低相似度（批次2-5）：top1 低于此值视为"问的社区不在图里"，
# 直接回退向量（不喂 LLM 确认，防无关问题硬匹配）——纯门槛而非判分依据
_COMMUNITY_SIM_THRESHOLD = 0.30
# 高置信快速路差距阈值（批次3）：top1 与 top2 相似度差超过此值 → 排序可信，
# 直接取 top1 跳过 LLM 确认（"置信度高信任算法、低时求助 LLM"的成本质量平衡）
_COMMUNITY_CONFIDENT_GAP = 0.15
# 常见问法结果缓存（批次3）：问句 → (分区版本, 写入时刻, 社区 id)，
# LRU 容量 + TTL 秒；批次4 修复：缓存条目带分区版本（社区重算后旧缓存
# 必须失效——此前旧缓存存的社区 id 在新分区里指向别的社区，
# "IT团队"匹配返回泛称簇即此 bug）
_COMMUNITY_CACHE: dict[str, tuple[int, float, int]] = {}
_COMMUNITY_CACHE_SIZE = 100
_COMMUNITY_CACHE_TTL = 300.0


async def _match_community_by_question(question: str) -> dict | None:
    """社区级问答（批次2-5 引入，批次3 加两级门控+缓存）：
    不点名问法（"研发团队有哪些人"）——embedding 召回 + 置信度门控：
    ①top1 与 top2 差距 > 0.15 → 排序可信，直接取 top1（零 LLM 调用）
    ②差距小（排序难分）→ LLM 从 top3 确认指代，不确认输出 null
    ③常见问法命中缓存（LRU，300s TTL，批次4 起带分区版本校验）。
    全部失败 → None 回退向量。"""
    try:
        communities = await neo4j_store.all_communities()
        if not communities:
            return None
        version = max(c.get("built_at", 0) for c in communities)

        cached = _community_cache_hit(question, version)
        if cached is not None:
            for c in communities:
                if c["id"] == cached:
                    return c
            return None  # 缓存的社区 id 已不在当前分区（理论不可达）

        from app.rag.embeddings import get_embeddings

        emb = get_embeddings()
        q_vec = emb.embed_query(question)
        doc_texts = [
            f"{c['summary']} 成员：{'、'.join(c['members'])}" for c in communities
        ]
        doc_vecs = emb.embed_documents(doc_texts)
        sims = [
            sum(a * b for a, b in zip(q_vec, d_vec)) for d_vec in doc_vecs
        ]
        ranked = sorted(
            zip(sims, communities), key=lambda x: x[0], reverse=True
        )
        if ranked[0][0] < _COMMUNITY_SIM_THRESHOLD:
            return None
        # 批次3 高置信快速路：差距大直接取 top1
        if len(ranked) >= 2 and ranked[0][0] - ranked[1][0] > _COMMUNITY_CONFIDENT_GAP:
            _community_cache_put(question, version, ranked[0][1]["id"])
            return ranked[0][1]
        if len(ranked) == 1:
            _community_cache_put(question, version, ranked[0][1]["id"])
            return ranked[0][1]

        top3 = [c for _, c in ranked[:3]]
        candidates_text = "\n".join(
            f"- 社区 {c['id'] + 1} | 成员：{'、'.join(c['members'])} | 概况：{c['summary']}"
            for c in top3
        )
        from app.rag.llm import get_llm
        from app.rag.graph.prompts import COMMUNITY_MATCH_PROMPT

        # 批次4：确定性判断类调用温度 0；批次5 A1：带超时
        from app.rag.llm import invoke_with_timeout

        llm = get_llm(streaming=False, temperature=0)
        resp = await invoke_with_timeout(
            llm,
            COMMUNITY_MATCH_PROMPT.format(
                candidates=candidates_text, question=question
            ),
        )
        if resp is None:
            return None
        text = resp.content if hasattr(resp, "content") else str(resp)
        data = json.loads(_strip_code_fence(text))
        picked = data.get("community")
        if not isinstance(picked, int):
            return None
        # 社区编号映射回真实 id（LLM 输出的是候选列表中的展示编号）
        for c in top3:
            if c["id"] + 1 == picked:
                _community_cache_put(question, version, c["id"])
                return c
        return None
    except Exception as e:
        logger.warning(f"社区级问答匹配失败，回退向量: {e}")
        return None


def _community_cache_hit(question: str, version: int) -> int | None:
    """缓存命中（LRU + TTL + 分区版本）：命中且版本一致返回社区 id；
    未命中/过期/分区已变返回 None"""
    import time as time_mod

    entry = _COMMUNITY_CACHE.get(question)
    if entry is None:
        return None
    cached_version, ts, cid = entry
    if cached_version != version:
        _COMMUNITY_CACHE.pop(question, None)  # 分区已变，旧缓存作废
        return None
    if time_mod.time() - ts > _COMMUNITY_CACHE_TTL:
        _COMMUNITY_CACHE.pop(question, None)
        return None
    return cid


def _community_cache_put(question: str, version: int, community_id: int) -> None:
    """写入缓存（LRU：超容量先淘汰最旧条目）"""
    import time as time_mod

    if len(_COMMUNITY_CACHE) >= _COMMUNITY_CACHE_SIZE and question not in _COMMUNITY_CACHE:
        oldest = min(_COMMUNITY_CACHE, key=lambda k: _COMMUNITY_CACHE[k][1])
        _COMMUNITY_CACHE.pop(oldest, None)
    _COMMUNITY_CACHE[question] = (version, time_mod.time(), community_id)


def _filter_neighbors_by_intent(rows: list[dict], intent: str | None) -> list[dict]:
    """按意图过滤邻居（边语义 A-[上级]->B 表示 B 是 A 的上级；LLM 亦可能抽
    "X的下属是Y"成 (X)-[下属]->(Y)，两方向都要覆盖）：
    - subordinates：对方是本实体的下级（in+上级 / out+下属）
    - superior：对方是本实体的上级（out+上级 / in+下属）
    - None：不过滤"""
    if intent == "subordinates":
        return [
            r for r in rows
            if (r["direction"] == "in" and r["relation"] == "上级")
            or (r["direction"] == "out" and r["relation"] == "下属")
        ]
    if intent == "superior":
        return [
            r for r in rows
            if (r["direction"] == "out" and r["relation"] == "上级")
            or (r["direction"] == "in" and r["relation"] == "下属")
        ]
    return rows


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


async def _rewrite_query(question: str) -> list[str]:
    """LLM 从问题抽实体名（≤5）"""
    try:
        from app.rag.llm import get_llm, invoke_with_timeout

        llm = get_llm(streaming=False)
        resp = await invoke_with_timeout(
            llm, QUERY_REWRITE_PROMPT.format(question=question)
        )
        if resp is None:
            raise TimeoutError("查询重写超时")
        text = (resp.content if hasattr(resp, "content") else str(resp))
        data = json.loads(_strip_code_fence(text))
        return [e.strip() for e in data.get("entities", []) if e and e.strip()][:5]
    except Exception as e:
        logger.warning(f"图谱查询重写失败，回退整句: {e}")
        return [question]


async def _link_entities_by_name(question: str) -> list[str]:
    """确定性实体链接（8 点改进 3 的第一层）：图节点名直接出现在问句中的
    取为实体（按问句出现顺序、最多 2 个）；别名/简称消歧留第二步"""
    names = await neo4j_store.all_entity_names()
    hits = []
    for name in sorted(names, key=len, reverse=True):  # 长名优先（防短名误吞）
        if len(name) >= 2 and name in question:
            pos = question.index(name)
            hits.append((pos, name))
    hits.sort(key=lambda x: x[0])
    seen = set()
    out = []
    for _, name in hits:
        # 跳过已被更长名覆盖的短名（如"王总"与"王建国"同位置）
        if any(name in other for other in out):
            continue
        if name not in seen:
            seen.add(name)
            out.append(name)
        if len(out) >= 2:
            break
    return out


def _vector_recall(mention: str, pool: list[dict], top_k: int = 10) -> list[dict]:
    """向量召回候选（批次2-2 消歧第一层）：称呼与图节点名做 embedding
    相似度排序取 top_k。大图（>50 节点）才走此路；小图全量喂 LLM 更准。
    纯阻塞调用（embedding API），调用方放线程池。"""
    try:
        from app.rag.embeddings import get_embeddings

        emb = get_embeddings()
        q_vec = emb.embed_query(mention)
        doc_vecs = emb.embed_documents([f"{n['name']} {n['description']}" for n in pool])
        # 余弦相似度（embedding 归一化后内积即余弦）
        sims = [
            sum(a * b for a, b in zip(q_vec, d_vec))
            for d_vec in doc_vecs
        ]
        ranked = sorted(
            zip(pool, sims), key=lambda x: x[1], reverse=True
        )
        return [p for p, _ in ranked[:top_k]]
    except Exception as e:
        logger.warning(f"向量召回失败，回退全量候选池: {e}")
        return pool[:_FULL_CANDIDATE_LIMIT]


async def _disambiguate(
    mentions: list[str], question: str
) -> dict[str, str | None]:
    """别名消歧（批次2-2 消歧第二层）：称呼 → 规范节点名映射。
    向量召回截断候选池后交 LLM 一次判断多个称呼；失败返回空映射（上层回退）。"""
    try:
        pool = await neo4j_store.entity_details()
        if not pool:
            return {}
        if len(pool) > _FULL_CANDIDATE_LIMIT:
            # 大图：向量召回截断（每个称呼取并集）
            recalled: dict[str, list[dict]] = {}
            for m in mentions:
                recalled[m] = await asyncio.to_thread(_vector_recall, m, pool)
            pool = {n["name"]: n for r in recalled.values() for n in r}.values()
        candidates_text = "\n".join(
            f"- {n['name']} | {n['type']} | {n['description']}" for n in pool
        )
        from app.rag.llm import get_llm

        # 批次4：消歧判断类调用温度 0（与社区匹配同理由）
        llm = get_llm(streaming=False, temperature=0)
        # 挂清单收敛：消歧 LLM 调用走专用池（原挂共享池）
        from app.rag.llm import run_in_llm_executor

        resp = await run_in_llm_executor(
            llm.invoke,
            DISAMBIGUATE_PROMPT.format(
                candidates=candidates_text,
                question=question,
                mentions=", ".join(mentions),
            ),
        )
        text = resp.content if hasattr(resp, "content") else str(resp)
        data = json.loads(_strip_code_fence(text))
        mapping = data.get("mapping") or {}
        valid_names = {n["name"] for n in pool}
        # 只接受映射到候选池内节点的结果；null/池外名一律置 None（防幻觉名）
        return {
            m: (v if isinstance(v, str) and v in valid_names else None)
            for m, v in mapping.items()
            if isinstance(m, str)
        }
    except Exception as e:
        logger.warning(f"实体消歧失败，回退不消歧: {e}")
        return {}


async def search(question: str, top_k: int = 6) -> dict | None:
    """图谱查询主入口。返回合成上下文 dict：
    {"entities": [...], "path": {...}|None, "neighbors": [...]}；
    实体链接失败（介入条件不满足）返回 None。"""
    if not await neo4j_store.is_available():
        return None

    # 实体链接：确定性名字匹配优先（LLM 重写偶发漏实体），
    # LLM 重写兜底；重写出的称呼再走别名消歧（"王总"→"王建国"，批次2-2）
    names = await _link_entities_by_name(question)
    if not names:
        mentions = await _rewrite_query(question)
        # 整句回退（重写失败）不参与消歧——消歧只对短称呼有意义
        if mentions and len(mentions[0]) <= 50:
            mapping = await _disambiguate(mentions, question)
            names = [mapping[m] for m in mentions if mapping.get(m)]
    if not names:
        # 批次2-5 社区级问答：无实体锚定但问团队/圈子（"研发团队有哪些人"）
        # → 问句向量匹配社区；匹配不上回退向量（介入条件不满足）
        if _has_community_intent(question):
            comm = await _match_community_by_question(question)
            if comm:
                return {
                    "entities": [], "path": None,
                    "neighbors": [], "community": comm,
                }
        return None

    entities = names[:2]  # 路径查询最多取前 2 个实体
    result: dict = {"entities": entities, "path": None, "neighbors": []}
    intent = _neighbor_intent(question)

    if len(entities) >= 2:
        path = await neo4j_store.find_path(entities[0], entities[1])
        if path is None:
            # 路径失败：降级为各自邻居（可能有别名导致的节点名偏差）
            for e in entities:
                result["neighbors"].extend(await neo4j_store.neighbors(e, limit=5))
            if not result["neighbors"]:
                return None
        else:
            result["path"] = path
            # 路径两端实体存在 → 介入条件满足
    else:
        # 批次2-3 分支①：递归上级问法（"郑浩的上级的上级是谁"）→ 定向多跳链
        # （跳数=问句"的上级"重复次数；层级不够回退最长链）
        if _has_recursive_superior(question):
            chain = await neo4j_store.find_chain(
                entities[0], hops=_superior_hops(question)
            )
            if chain:
                result["path"] = chain
                return result
            # 无上级链 → 落回邻居分支（下方继续）
        # 批次2-3 分支②：聚合问法（"周涛的团队有哪些人"）→ 社区
        if _has_community_intent(question):
            comm = await neo4j_store.entity_community(entities[0])
            if comm:
                result["community"] = comm
                return result
            # 实体无社区（社区检测未跑/无分区）→ 落回邻居分支
        # limit 加宽到 ≥10（补强：问"下属/上级"的邻居期望与 limit=top_k 同宽时，
        # 多一条同强度边即截断误判；查询先取足、再按意图过滤）
        nb = await neo4j_store.neighbors(entities[0], limit=max(top_k, 10))
        # 按问题意图过滤方向（补强：问"下属"不再混入上级/负责边）
        nb = _filter_neighbors_by_intent(nb, intent)
        if not nb:
            return None  # 实体在图里查无该意图邻居 → 图谱帮不上忙，回退向量
        result["neighbors"] = nb

    # 介入条件：路径存在 或 邻居非空（上面已保证）
    return result


async def entity_context_for_text(text: str, limit: int = 3) -> dict | None:
    """按文本反查图实体并取邻居（批次2-5e：规则审批人"财务总监"→实体赵明远
    → 挂"其上级是王建国"图谱上下文）。先名字精确匹配、再描述包含匹配；
    查不到（如"直属上级"无对应人）返回 None（静默跳过）。"""
    names = await neo4j_store.all_entity_names()
    hit_name: str | None = None
    if text in names:
        hit_name = text
    else:
        details = await neo4j_store.entity_details()
        for d in details:
            if text in (d["description"] or "") or (
                d["description"] and d["description"] in text
            ):
                hit_name = d["name"]
                break
    if not hit_name:
        return None
    nb = await neo4j_store.neighbors(hit_name, limit=limit)
    if not nb:
        return None
    return {"entity": hit_name, "neighbors": nb}


def format_context(result: dict) -> str:
    """组织图谱上下文文本（实体表 + 路径/关系表 + 社区块（批次2-3））"""
    lines = ["---- 知识图谱（[KG] 引用） ----"]
    if result.get("path"):
        # steps 已由 find_path 按边方向生成定向陈述（反向遍历也已校正）
        lines.append(f"关系链（{result['path']['length']} 跳）：" + "；".join(result["path"]["steps"]) + "。")
    if result.get("community"):
        comm = result["community"]
        # 实体锚定（"周涛的团队"）与社区级（"研发团队有哪些人"，批次2-5）
        # 两种问法复用同一社区块；无实体时措辞为"匹配到的社区"
        prefix = (
            f"实体 {result['entities'][0]} 所在社区"
            if result.get("entities") else "匹配到的社区"
        )
        lines.append(
            f"{prefix}（Leiden 连接密度检测）：成员 {comm['members']}，共 {comm['count']} 人。"
            f"社区概况：{comm['summary']}"
        )
    if result.get("neighbors"):
        lines.append("关联实体：")
        for n in result["neighbors"]:
            # 边语义约定：A-[上级]->B 表示"B 是 A 的上级"（抽取自"X的上级是Y"）
            # 出边=对方是本实体的该关系；入边=本实体是对方的该关系
            label = (
                f"本实体是对方的{n['relation']}"
                if n["direction"] == "in"
                else f"对方是本实体的{n['relation']}"
            )
            lines.append(f"- {n['name']}（{label}，强度 {n['strength']}）")
    lines.append("请依据图谱关系回答，引用标注 [KG]；图谱与文档冲突时以文档为准。")
    return "\n".join(lines)
