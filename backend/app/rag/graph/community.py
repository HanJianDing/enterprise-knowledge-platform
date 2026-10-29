"""图谱社区检测与社区报告（批次2-3）——Leiden 算法 + LLM 社区总结

- detect_communities：Neo4j 拉本 env 实体+关系 → 无向化 → igraph
  community_leiden（modularity 目标）分区 → {实体名: 社区号}
  （Leiden 与 embedding 聚类的区别：这里看的是"谁和谁连着"的图连接
  密度，不是文本语义——自动发现"财务圈""研发圈"无需预知部门划分）
- generate_community_reports：每个社区 LLM 生成报告（成员/关系结构/角色）
  写 :Community 节点（幂等：先删本 env 旧社区节点再写）
- run_community_detection：两步串联，全程 try/except（增强腿哲学：
  失败只留日志，不阻断主流程）
"""
import asyncio
import logging

from app.config import settings
from app.rag.graph import neo4j_store

logger = logging.getLogger(__name__)

_COMMUNITY_PROMPT = """你是企业组织分析助手。知识图谱检测出一个紧密联系的实体社区
（成员之间汇报/审批/负责关系密集）。请用 2~3 句话总结这个社区：
1. 社区核心是谁、大致层级结构
2. 社区整体负责什么职能（从成员描述推断）——必须写出职能关键词
   （如"研发""财务""IT 运维"等），供后续检索召回使用

社区成员（名称 | 类型 | 描述）：
{members}

关系列表（三元组文本）：
{relations}

只输出总结文字，不要任何解释或标记。"""


async def detect_communities() -> dict[str, int]:
    """Leiden 分区：返回 {实体名: 社区号}（社区号从 0 递增，按规模降序）。
    图为空/igraph 不可用时返回空 dict（上层静默跳过）。"""
    try:
        import igraph

        # 拉本 env 实体与关系（小图规模，全量内存构图）
        driver = neo4j_store._get_driver()
        if driver is None:
            return {}
        env = neo4j_store._env()
        async with driver.session() as session:
            result = await session.run(
                "MATCH (a:Entity {env: $env})-[r:REL]->(b:Entity) "
                "RETURN a.name AS src, b.name AS dst, r.relation AS relation, "
                "r.strength AS strength",
                env=env,
            )
            edges = [
                (rec["src"], rec["dst"], rec["relation"], rec["strength"])
                async for rec in result
            ]
        if not edges:
            return {}

        names: list[str] = []
        name_to_idx: dict[str, int] = {}
        for src, dst, _, _ in edges:
            for n in (src, dst):
                if n not in name_to_idx:
                    name_to_idx[n] = len(names)
                    names.append(n)

        g = igraph.Graph(n=len(names), directed=False)
        g.vs["name"] = names
        g.add_edges([(name_to_idx[s], name_to_idx[d]) for s, d, _, _ in edges])
        # strength 作边权（连接越强越应该同社区）
        g.es["weight"] = [s for _, _, _, s in edges]

        # resolution=1.0（默认）为实测最优平衡（批次2-5 调参实验，43 节点图）：
        # 0.5 → 5 社区但王建国枢纽社区吞 12 人（人事+财务圈整体被卷）
        # 1.0 → 7 社区，四个部门圈清晰独立（当前采用）
        # 1.5 → 9 社区，研发圈独立但市场圈被吞、碎社区增多
        # 结论：枢纽节点（总经理连所有人）没有完美 resolution——modularity
        # 优化的是连接密度，枢纽天然粘某圈；要彻底解耦需图结构处理
        # （删枢纽重跑），非调参可解。1.0 为平衡点
        partition = g.community_leiden(
            objective_function="modularity",
            weights="weight",
            resolution=1.0,
            n_iterations=2,
        )
        # 社区号重排：按规模降序（大社区先编号，输出稳定）
        sizes: dict[int, int] = {}
        for cid in partition.membership:
            sizes[cid] = sizes.get(cid, 0) + 1
        ordered = sorted(sizes, key=lambda c: -sizes[c])
        remap = {cid: i for i, cid in enumerate(ordered)}
        return {name: remap[cid] for name, cid in zip(names, partition.membership)}
    except ImportError:
        logger.warning("igraph 未安装，跳过社区检测")
        return {}
    except Exception as e:
        logger.warning(f"社区检测失败: {e}")
        return {}


async def _community_members_and_relations(
    partition: dict[str, int],
) -> dict[int, dict]:
    """按分区聚合成员明细与关系文本（供报告生成与存储）"""
    driver = neo4j_store._get_driver()
    env = neo4j_store._env()
    details = await neo4j_store.entity_details()
    desc_by_name = {d["name"]: (d["type"], d["description"]) for d in details}

    communities: dict[int, dict] = {}
    for name, cid in partition.items():
        communities.setdefault(cid, {"members": [], "relations": []})
        t, desc = desc_by_name.get(name, ("", ""))
        communities[cid]["members"].append(f"{name} | {t} | {desc}")

    async with driver.session() as session:
        result = await session.run(
            "MATCH (a:Entity {env: $env})-[r:REL]->(b:Entity) "
            "RETURN a.name AS src, b.name AS dst, r.relation AS relation",
            env=env,
        )
        async for rec in result:
            cid = partition.get(rec["src"])
            if cid is not None and partition.get(rec["dst"]) == cid:
                # 社区内部边（跨社区边不进报告，避免社区总结混入外部噪音）
                communities[cid]["relations"].append(
                    f"{rec['src']}的{rec['relation']}是{rec['dst']}"
                )
    return communities


async def generate_community_reports(partition: dict[str, int]) -> int:
    """每个社区 LLM 生成报告 → 写 :Community 节点（幂等：先删本 env 旧节点）。
    返回写入的社区数。"""
    if not partition:
        return 0
    communities = await _community_members_and_relations(partition)
    driver = neo4j_store._get_driver()
    env = neo4j_store._env()

    reports: dict[int, str] = {}
    from app.rag.llm import get_llm, invoke_with_timeout

    llm = get_llm(streaming=False)
    for cid, data in communities.items():
        try:
            # 批次5 A1：报告生成带超时——超时放弃该社区（占位文案），
            # 不阻塞其余社区与整体检测流程
            resp = await invoke_with_timeout(
                llm,
                _COMMUNITY_PROMPT.format(
                    members="\n".join(data["members"]),
                    relations="\n".join(data["relations"] or ["（无内部关系）"]),
                ),
            )
            if resp is None:
                raise TimeoutError("社区报告生成超时")
            text = resp.content if hasattr(resp, "content") else str(resp)
            # 质检#4：剥离 markdown 围栏（LLM 偶发输出 ``` 包裹，复用抽取器同款工具）
            from app.rag.graph.extractor import _strip_code_fence

            reports[cid] = _strip_code_fence(text)
        except Exception as e:
            logger.warning(f"社区 {cid} 报告生成失败: {e}")
            reports[cid] = "（报告生成失败）"

    async with driver.session() as session:
        await session.run(
            "MATCH (c:Community {env: $env}) DELETE c", env=env
        )
        for cid, summary in reports.items():
            members = ", ".join(
                m.split(" | ")[0] for m in communities[cid]["members"]
            )
            await session.run(
                "MERGE (c:Community {community_id: $cid, env: $env}) "
                "SET c.summary = $summary, c.members = $members, "
                "c.member_count = $count, c.built_at = timestamp()",
                cid=int(cid), env=env, summary=summary,
                members=members, count=len(communities[cid]["members"]),
            )
            # 社区号写回实体节点（查询链路按实体直接取社区，免字符串匹配误伤）
            member_names = [m.split(" | ")[0] for m in communities[cid]["members"]]
            await session.run(
                "MATCH (n:Entity {env: $env}) WHERE n.name IN $names "
                "SET n.community_id = $cid",
                env=env, names=member_names, cid=int(cid),
            )
    return len(reports)


async def run_community_detection() -> dict:
    """社区检测主入口：Leiden 分区 → 报告生成。全程 try/except；
    返回 {partition, community_count}。图谱不可用/无关系时返回空结果。"""
    try:
        if not settings.graphrag_enabled or not await neo4j_store.is_available():
            return {"partition": {}, "community_count": 0}
        partition = await detect_communities()
        if not partition:
            return {"partition": {}, "community_count": 0}
        count = await generate_community_reports(partition)
        logger.info(f"社区检测完成: {count} 个社区")
        return {"partition": partition, "community_count": count}
    except Exception as e:
        logger.warning(f"社区检测流程失败: {e}")
        return {"partition": {}, "community_count": 0}


async def graph_overview() -> dict:
    """图谱可视化数据（批次2-3 可视化页）：节点（含社区号）/边/社区报告。
    图谱不可用返回空结构（前端展示空态）。"""
    empty = {"nodes": [], "edges": [], "communities": []}
    driver = neo4j_store._get_driver()
    if driver is None or not await neo4j_store.is_available():
        return empty
    try:
        env = neo4j_store._env()
        async with driver.session() as session:
            result = await session.run(
                "MATCH (n:Entity {env: $env}) "
                "RETURN n.name AS name, n.type AS type, "
                "n.description AS description, n.community_id AS community_id",
                env=env,
            )
            nodes = [
                {
                    "name": rec["name"],
                    "type": rec["type"] or "",
                    "description": rec["description"] or "",
                    "community_id": rec["community_id"],
                }
                async for rec in result
            ]
            result = await session.run(
                "MATCH (a:Entity {env: $env})-[r:REL]->(b:Entity) "
                "RETURN a.name AS src, b.name AS dst, "
                "r.relation AS relation, r.strength AS strength",
                env=env,
            )
            edges = [
                {
                    "source": rec["src"],
                    "target": rec["dst"],
                    "relation": rec["relation"],
                    "strength": rec["strength"],
                }
                async for rec in result
            ]
            result = await session.run(
                "MATCH (c:Community {env: $env}) "
                "RETURN c.community_id AS id, c.members AS members, "
                "c.summary AS summary, c.member_count AS count "
                "ORDER BY c.community_id",
                env=env,
            )
            communities = [
                {
                    "id": rec["id"],
                    "members": [m.strip() for m in rec["members"].split(",") if m.strip()],
                    "summary": rec["summary"],
                    "count": rec["count"],
                }
                async for rec in result
            ]
        return {"nodes": nodes, "edges": edges, "communities": communities}
    except Exception as e:
        logger.warning(f"图谱概览查询失败: {e}")
        return empty
