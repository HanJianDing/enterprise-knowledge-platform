"""Neo4j 存储与遍历（验收批次2）——neo4j async driver

- 写入：MERGE 实体节点（带 source_id/chunk_id 证据、类型）、MERGE 关系边
  （带 strength/source_id 证据）
- 按文档删子图：删除该 source_id 的边与孤立节点（增量更新）
- 路径查询：两实体间 shortestPath（≤3 跳），返回路径文字链
- 邻居查询：单实体的 1 跳邻居（用于"XX 的下属是谁"类问题）
- 批次2-2 方案C：节点带 env 租户标签——演示/真实两环境共用同一实例时
  按 env 隔离（节点 MERGE 键=name+env；全部读写 Cypher 带 env 过滤）
"""
import logging

from app.config import settings

logger = logging.getLogger(__name__)

# 批次4 异常收窄：Neo4j 连接类故障静默降级（查询函数返回空/None），
# 编程错误自然抛出。neo4j 包未安装时降级为 Exception 兜底（惰性兼容）
try:
    from neo4j.exceptions import Neo4jError
except ImportError:  # neo4j 未安装 → 无连接可发生，兜底异常类
    Neo4jError = Exception

_driver = None


def _env() -> str:
    """当前环境租户标签（惰性读 settings，支持测试 monkeypatch 切换环境）"""
    return settings.neo4j_env


def _get_driver():
    """惰性单例 driver；配置缺失/驱动未安装时返回 None（上层降级）"""
    global _driver
    if _driver is not None:
        return _driver
    if not settings.neo4j_password:
        return None
    try:
        from neo4j import AsyncGraphDatabase
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        return _driver
    except Exception as e:  # neo4j 包未安装等
        logger.warning(f"Neo4j driver 初始化失败: {e}")
        return None


async def is_available() -> bool:
    driver = _get_driver()
    if driver is None:
        return False
    try:
        async with driver.session() as session:
            await session.run("RETURN 1")
        return True
    except Exception:
        return False


async def upsert_document_graph(
    doc_id: str,
    filename: str,
    entities: list[dict],
    relations: list[dict],
) -> int:
    """把一份文档的抽取结果写入 Neo4j（幂等：同 doc_id 先删旧图再写）；
    节点/边带 source_id + filename 证据（8 点改进 4：块级证据溯源）。
    批次2-2：节点 MERGE 键 = name+env（两环境共用实例时同名实体互不可见）。
    返回写入的关系边数。"""
    driver = _get_driver()
    if driver is None:
        return 0

    env = _env()
    async with driver.session() as session:
        # 幂等：先删本文件旧图（doc_id 全局唯一，两环境不会撞 source_id；
        # 边只存在于本 env 节点之间——写侧 MERGE 键保证，删除无需 env 过滤）
        await session.run(
            "MATCH ()-[r]->() WHERE r.source_id = $doc_id DELETE r",
            doc_id=doc_id,
        )
        await session.run(
            "MATCH (n:Entity) WHERE n.source_id = $doc_id AND n.env = $env "
            "AND NOT (n)--() DELETE n",
            doc_id=doc_id, env=env,
        )
        for e in entities:
            await session.run(
                "MERGE (n:Entity {name: $name, env: $env}) "
                "SET n.type = $type, n.description = $desc, "
                "n.source_id = $doc_id, n.filename = $filename",
                name=e["name"], type=e["type"], desc=e["description"],
                doc_id=doc_id, filename=filename, env=env,
            )
        for r in relations:
            # R2 修复：MERGE 端点时补默认属性——关系端点不在实体清单时
            # 也会建节点，缺 source_id 会成删不掉的孤儿、污染实体链接
            await session.run(
                "MERGE (a:Entity {name: $src, env: $env}) "
                "SET a.source_id = coalesce(a.source_id, $doc_id) "
                "MERGE (b:Entity {name: $dst, env: $env}) "
                "SET b.source_id = coalesce(b.source_id, $doc_id) "
                "MERGE (a)-[rel:REL {relation: $relation}]->(b) "
                "SET rel.strength = $strength, rel.source_id = $doc_id",
                src=r["source"], dst=r["target"],
                relation=r["relation"], strength=r["strength"],
                doc_id=doc_id, env=env,
            )
        # 建图标记节点：作为"已建图"状态源（批次2-2 启动补建依据）。
        # 放在最后 MERGE——中途异常时旧图已删、标记不存在 → 状态=未建，补建重试
        await session.run(
            "MERGE (m:GraphBuild {doc_id: $doc_id, env: $env}) "
            "SET m.filename = $filename, m.built_at = timestamp()",
            doc_id=doc_id, env=env, filename=filename,
        )
    return len(relations)


async def delete_document_graph(doc_id: str) -> int:
    """文档删除时清理其子图（增量更新）"""
    driver = _get_driver()
    if driver is None:
        return 0
    env = _env()
    async with driver.session() as session:
        result = await session.run(
            "MATCH ()-[r]->() WHERE r.source_id = $doc_id DELETE r "
            "RETURN count(r) AS n",
            doc_id=doc_id,
        )
        n = (await result.single())["n"] if await result.peek() else 0
        await session.run(
            "MATCH (n:Entity) WHERE n.source_id = $doc_id AND n.env = $env "
            "AND NOT (n)--() DELETE n",
            doc_id=doc_id, env=env,
        )
        # 建图标记随子图一并清理（否则文档重建会被误判"已建"）
        await session.run(
            "MATCH (m:GraphBuild {doc_id: $doc_id, env: $env}) DELETE m",
            doc_id=doc_id, env=env,
        )
    return n


async def find_path(entity_a: str, entity_b: str, max_hops: int = 3) -> dict | None:
    """两实体间最短路径（8 点改进 1：路径查询当主打）——
    返回 {"steps": ["A的上级是B", ...], "length": 跳数}（方向感知定向陈述）；
    无路径返回 None"""
    driver = _get_driver()
    if driver is None:
        return None
    try:
        env = _env()
        async with driver.session() as session:
            # 注意：Cypher 变长路径的跳数上限不支持参数绑定（[*..$hops] 语法错误），
            # 用受控的 int 字面量（max_hops 为内部参数，无注入面）；
            # 两端节点按 env 锚定——边只存在于同 env 节点间（写侧 MERGE 键保证），
            # 中间节点无需重复过滤。
            # 批次3：路径只沿"上级"边——汇报链语义（find_chain 同款）；
            # 此前无过滤时"同事/负责"边（部门职责文档并列列举被抽取）会
            # 截短路径（"许静的上级是张伟"被"许静是张伟的同事"1 跳替代）
            result = await session.run(
                f"MATCH p = shortestPath((a:Entity {{name: $a, env: $env}})-"
                f"[r:REL*..{int(max_hops)}]-(b:Entity {{name: $b, env: $env}})) "
                "WHERE ALL(x IN r WHERE x.relation = '上级') "
                "RETURN p",
                a=entity_a, b=entity_b, env=env,
            )
            record = await result.single()
            if record is None:
                return None
            path = record["p"]
            # 方向感知（质检 P2 + 批次2-2 补审）：无向遍历可能反向穿越边，逐跳比对
            # rel.start_node 与路径节点，生成定向陈述（边语义=A-[上级]->B 表示 B 是 A 的上级）。
            # 反向穿越时 start=路径后一节点 nb，即边为 nb-[r]->na → na 是 nb 的 r：
            # 渲染 f"{na}是{nb}的{relation}"（如路径段[王建国,刘强] + 边(刘强)-[上级]->(王建国)
            # → "王建国是刘强的上级"——真值"刘强的上级是王建国" ✓）
            steps = []
            for i, rel in enumerate(path.relationships):
                na = path.nodes[i]["name"]
                nb = path.nodes[i + 1]["name"]
                relation = rel["relation"]
                if rel.start_node["name"] == na:
                    steps.append(f"{na}的{relation}是{nb}")
                else:
                    steps.append(f"{na}是{nb}的{relation}")
            return {"steps": steps, "length": len(path.relationships)}
    except Neo4jError as e:
        logger.warning(f"图谱路径查询失败: {e}")
        return None


async def find_chain(entity: str, hops: int | None = None, max_hops: int = 3) -> dict | None:
    """单实体沿"上级"边定向多跳展开（批次2-3）："郑浩的上级的上级是谁"类
    递归问法——从实体出发沿关系=上级的出边走 1~max_hops 跳。hops 给定时
    优先返回恰好 hops 跳的链（问句"的上级"重复次数=期望跳数）；无恰好链
    （实体层级不够）回退最长链。steps 全为正向遍历；无链返回 None"""
    driver = _get_driver()
    if driver is None:
        return None
    try:
        env = _env()
        async with driver.session() as session:
            # 定向变长查询：所有边关系名必须是"上级"（跳数 int 字面量，
            # 同 find_path 的注入面说明）；取全部链在 Python 侧选跳数
            result = await session.run(
                f"MATCH p = (a:Entity {{name: $name, env: $env}})-[r:REL*1..{int(max_hops)}]->(end) "
                "WHERE ALL(x IN r WHERE x.relation = '上级') "
                "RETURN p ORDER BY length(p) DESC",
                name=entity, env=env,
            )
            paths = [rec["p"] async for rec in result]
        if not paths:
            return None
        if hops is not None:
            exact = [p for p in paths if len(p.relationships) == hops]
            path = exact[0] if exact else paths[0]
        else:
            path = paths[0]
        steps = []
        for i, rel in enumerate(path.relationships):
            na = path.nodes[i]["name"]
            nb = path.nodes[i + 1]["name"]
            steps.append(f"{na}的{rel['relation']}是{nb}")
        return {"steps": steps, "length": len(path.relationships)}
    except Neo4jError as e:
        logger.warning(f"图谱定向链查询失败: {e}")
        return None


async def all_communities() -> list[dict]:
    """本 env 全量社区（批次2-5 社区级问答候选池：不点名问法
    "研发团队有哪些人"按社区报告/成员做向量匹配）"""
    driver = _get_driver()
    if driver is None:
        return []
    try:
        env = _env()
        async with driver.session() as session:
            result = await session.run(
                "MATCH (c:Community {env: $env}) "
                "RETURN c.community_id AS id, c.summary AS summary, "
                "c.members AS members, c.member_count AS count, "
                "c.built_at AS built_at "
                "ORDER BY c.community_id",
                env=env,
            )
            return [
                {
                    "id": rec["id"],
                    "summary": rec["summary"],
                    # 质检 O8 防御：members 异常为 null 时给空列表，不让单节点拖垮整体
                    "members": [m.strip() for m in (rec["members"] or "").split(",") if m.strip()],
                    "count": rec["count"],
                    # 分区版本（批次4 缓存失效校验：社区重算后旧缓存必须失效）
                    "built_at": rec["built_at"] or 0,
                }
                async for rec in result
            ]
    except Neo4jError as e:
        logger.warning(f"社区列表查询失败: {e}")
        return []


async def entity_community(entity: str) -> dict | None:
    """实体所在社区（批次2-3）：社区号 + 成员 + 报告（社区检测后才有数据）"""
    driver = _get_driver()
    if driver is None:
        return None
    try:
        env = _env()
        async with driver.session() as session:
            result = await session.run(
                "MATCH (n:Entity {name: $name, env: $env}) "
                "WHERE n.community_id IS NOT NULL "
                "WITH n.community_id AS cid "
                "MATCH (c:Community {community_id: cid, env: $env}) "
                "RETURN c.community_id AS id, c.summary AS summary, "
                "c.members AS members, c.member_count AS count",
                name=entity, env=env,
            )
            record = await result.single()
            if record is None:
                return None
            return {
                "id": record["id"],
                # 质检防御：summary/members 异常为 null 时兜底（写入路径恒非 null）
                "summary": record["summary"] or "",
                "members": [m.strip() for m in (record["members"] or "").split(",") if m.strip()],
                "count": record["count"] or 0,
            }
    except Neo4jError as e:
        logger.warning(f"实体社区查询失败: {e}")
        return None


async def all_entity_names() -> list[str]:
    """本 env 全量实体名（小图规模；实体链接用确定性匹配，别名消歧见 query.py）"""
    driver = _get_driver()
    if driver is None:
        return []
    try:
        async with driver.session() as session:
            result = await session.run(
                "MATCH (n:Entity) WHERE n.env = $env RETURN n.name AS name",
                env=_env(),
            )
            return [rec["name"] async for rec in result]
    except Neo4jError as e:
        logger.warning(f"实体名查询失败: {e}")
        return []


async def entity_details() -> list[dict]:
    """本 env 全量实体明细（name/type/description）——批次2-2 消歧候选池：
    小图全量喂 LLM，大图由 query.py 向量召回截断"""
    driver = _get_driver()
    if driver is None:
        return []
    try:
        async with driver.session() as session:
            result = await session.run(
                "MATCH (n:Entity) WHERE n.env = $env "
                "RETURN n.name AS name, n.type AS type, n.description AS description",
                env=_env(),
            )
            return [
                {
                    "name": rec["name"],
                    "type": rec["type"] or "",
                    "description": rec["description"] or "",
                }
                async for rec in result
            ]
    except Neo4jError as e:
        logger.warning(f"实体明细查询失败: {e}")
        return []


async def neighbors(entity: str, limit: int = 10) -> list[dict]:
    """单实体 1 跳邻居（带方向：出边=本实体对对方的关系，入边=对方对本实体的关系）——
    修复无向查询把"下属"也列成"上级"的方向混淆；批次2-2 按 env 过滤"""
    driver = _get_driver()
    if driver is None:
        return []
    try:
        env = _env()
        async with driver.session() as session:
            result = await session.run(
                "MATCH (a:Entity {name: $name, env: $env})-[r:REL]->(b:Entity) "
                "RETURN b.name AS name, r.relation AS relation, r.strength AS strength, 'out' AS dir "
                "UNION "
                "MATCH (a:Entity {name: $name, env: $env})<-[r:REL]-(b:Entity) "
                "RETURN b.name AS name, r.relation AS relation, r.strength AS strength, 'in' AS dir",
                name=entity, env=env,
            )
            rows = [
                {
                    "name": rec["name"],
                    "relation": rec["relation"],
                    "strength": rec["strength"],
                    "direction": rec["dir"],
                }
                async for rec in result
            ]
            rows.sort(key=lambda x: x["strength"], reverse=True)
            return rows[:limit]
    except Neo4jError as e:
        logger.warning(f"图谱邻居查询失败: {e}")
        return []


async def migrate_legacy_nodes() -> int:
    """一次性迁移：env 标签上线前的存量节点打上当前环境标签（幂等，无存量时为 0）。
    仅应在演示环境调用（存量数据全部产生于演示环境）；真实环境启用时
    存量节点早已带 env 属性，此函数不会误标。"""
    driver = _get_driver()
    if driver is None:
        return 0
    try:
        async with driver.session() as session:
            result = await session.run(
                "MATCH (n:Entity) WHERE n.env IS NULL SET n.env = $env "
                "RETURN count(n) AS n",
                env=_env(),
            )
            record = await result.single()
            return record["n"] if record else 0
    except Neo4jError as e:
        logger.warning(f"图谱存量节点迁移失败: {e}")
        return 0


async def has_document_graph(doc_id: str) -> bool:
    """文档是否已建图（批次2-2 启动补建的状态源——查建图标记节点而非实体节点：
    0 实体的文档（纯表格/无人物关系）也有标记，不会被误判"未建"而反复重建）"""
    driver = _get_driver()
    if driver is None:
        return False
    try:
        async with driver.session() as session:
            result = await session.run(
                "MATCH (m:GraphBuild {doc_id: $doc_id, env: $env}) "
                "RETURN m LIMIT 1",
                doc_id=doc_id, env=_env(),
            )
            return await result.single() is not None
    except Neo4jError as e:
        logger.warning(f"图谱建图状态检查失败: {e}")
        return False
