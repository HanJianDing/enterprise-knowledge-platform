"""建图服务（验收批次2）——文档 → 抽取 → Neo4j

- build_graph_for_document：文档分块完成后调用（document_service 挂钩，
  fire-and-forget；失败留日志不阻断主流程——图谱是增强腿，不是命脉）
- 幂等：upsert 前按 doc_id 删旧图
- delete_graph_for_document：文档删除时同步清理（增量更新，8 点改进配套）
"""
import asyncio
import logging

from app.config import settings
from app.rag.graph import neo4j_store
from app.rag.graph.extractor import build_extract_prompt, pack_batches, parse_extraction

logger = logging.getLogger(__name__)


async def build_graph_for_document(doc_id: str, filename: str) -> int:
    """取文档分块 → 批打包 → LLM 抽取 → 过滤 → 写图。返回写入关系数。

    全程 try/except（质检补审：Neo4j 中途断连等异常只留日志，
    与 delete 侧风格对齐，防 "Task exception was never retrieved" 噪音）。
    """
    try:
        if not settings.graphrag_enabled:
            return 0
        if not await neo4j_store.is_available():
            logger.info(f"Neo4j 不可用，跳过建图: {filename}")
            return 0

        from app.rag.retriever import get_chunks_by_document

        chunks = get_chunks_by_document(doc_id)
        texts = [c["content"] for c in chunks if c.get("content")]
        if not texts:
            return 0

        batches = pack_batches(texts)
        entities: list[dict] = []
        relations: list[dict] = []
        from app.rag.llm import get_llm

        from app.rag.llm import invoke_with_timeout

        llm = get_llm(streaming=False)
        for batch in batches:
            # 批次5 A1：抽取带超时——超时放弃该批（宁缺勿滥，不阻塞建图）
            resp = await invoke_with_timeout(llm, build_extract_prompt(batch))
            if resp is None:
                continue
            text = resp.content if hasattr(resp, "content") else str(resp)
            parsed = parse_extraction(text)
            entities.extend(parsed["entities"])
            relations.extend(parsed["relations"])

        # 同名实体去重（保留首次描述；跨文档合并交给 Neo4j MERGE 节点属性覆盖策略）
        seen: set[str] = set()
        deduped_entities = []
        for e in entities:
            if e["name"] not in seen:
                seen.add(e["name"])
                deduped_entities.append(e)

        # 批次2-2 收紧①：role（岗位角色）类型实体不参与关系——LLM 会把
        # "IT 运维工程师：郑浩"抽成 role 节点并造边，混进"郑浩的上级是谁"
        # 的邻居成噪音；图的查询价值在人与人/人与部门的汇报审批链，
        # 角色信息在实体描述里由向量检索覆盖
        type_by_name = {e["name"]: e["type"] for e in deduped_entities}
        relations = [
            r for r in relations
            if type_by_name.get(r["source"]) in ("person", "department")
            and type_by_name.get(r["target"]) in ("person", "department")
        ]
        # 批次2-2 收紧②：只保留关系端点实体——孤立实体（抽取了但没进
        # 任何三元组）只占节点不产生价值
        endpoint_names = {r["source"] for r in relations} | {
            r["target"] for r in relations
        }
        deduped_entities = [e for e in deduped_entities if e["name"] in endpoint_names]

        count = await neo4j_store.upsert_document_graph(
            doc_id, filename, deduped_entities, relations
        )
        logger.info(f"图谱建图完成: {filename} → {len(deduped_entities)} 实体 / {count} 关系")
        return count
    except Exception as e:
        logger.warning(f"图谱建图失败（文档 {filename}）: {e}")
        return 0


async def delete_graph_for_document(doc_id: str) -> int:
    """文档删除 → 清理其子图"""
    if not settings.graphrag_enabled:
        return 0
    return await neo4j_store.delete_document_graph(doc_id)


async def rebuild_missing_graphs(max_docs: int = 20) -> int:
    """启动补建（批次2-2 job 化）：扫描 completed 文档，图中无标记者重建。
    以 Neo4j 标记节点为状态源（无需 job 表）——重启丢失/上次失败的建图任务
    在此补齐；幂等（重复跑无副作用）。每批最多补 max_docs 份防启动期
    长时间烧 LLM，超额留待下次启动继续（自愈式补齐）。
    返回本次尝试补建的文档数（含抽取后 0 关系者；单份失败由 build 内部留日志）"""
    if not settings.graphrag_enabled:
        return 0
    if not await neo4j_store.is_available():
        logger.info("Neo4j 不可用，跳过启动补建")
        return 0
    try:
        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.document import Document

        async with async_session_factory() as db:
            result = await db.execute(
                select(Document.id, Document.filename).where(
                    Document.status == "completed"
                )
            )
            docs = [(row[0], row[1]) for row in result.all()]
    except Exception as e:
        logger.warning(f"启动补建扫描文档失败: {e}")
        return 0

    count = 0
    for doc_id, filename in docs:
        if await neo4j_store.has_document_graph(doc_id):
            continue
        count += 1
        logger.info(f"启动补建图谱: {filename}")
        await build_graph_for_document(doc_id, filename)
        if count >= max_docs:
            logger.info(f"启动补建达到本批上限 {max_docs}，剩余留待下次启动")
            break
    if count > 0:
        # 批次2-3：补建后图结构变化 → 社区重算（增强腿，失败只留日志）
        from app.rag.graph import community as community_service

        await community_service.run_community_detection()
    return count
