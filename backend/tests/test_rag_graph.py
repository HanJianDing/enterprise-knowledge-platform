"""验收批次2 测试：图谱抽取器/受控词表/查询组织（Neo4j 交互用 mock）

批次2-2 增补：噪音实体过滤 / 别名消歧 / env 租户标签传递 / 关系层评估打分
"""
import pytest

from app.rag.graph.extractor import (
    _normalize_relation,
    build_extract_prompt,
    pack_batches,
    parse_extraction,
)
from app.rag.graph.query import format_context


class TestRelationVocab:
    def test_exact_alias_maps(self):
        assert _normalize_relation("上级") == "上级"
        assert _normalize_relation("直属上级") == "上级"
        assert _normalize_relation("审批") == "审批"

    def test_fuzzy_contains_maps(self):
        assert _normalize_relation("上级是") == "上级"
        assert _normalize_relation("负责管理") == "下属"

    def test_unknown_dropped(self):
        assert _normalize_relation("热爱") is None
        assert _normalize_relation("") is None


class TestPackBatches:
    def test_small_chunks_merged(self):
        assert pack_batches(["短", "短", "短"]) == ["短\n短\n短"]

    def test_oversize_splits(self):
        batches = pack_batches(["长内容" * 400, "长内容" * 400])  # 各 1200 字
        # 合起来 2401 > 2000 → 拆成两批，各在预算内
        assert len(batches) == 2
        assert all(len(b) <= 2000 for b in batches)


class TestParseExtraction:
    def test_entity_type_whitelist(self):
        """非白名单类型实体被丢弃"""
        text = ('{"entities": [{"name": "王建国", "type": "person", "description": "总经理"},'
                ' {"name": "会议室", "type": "room", "description": "三楼"}], "relations": []}')
        parsed = parse_extraction(text)
        assert [e["name"] for e in parsed["entities"]] == ["王建国"]

    def test_relation_vocab_filter(self):
        """关系不在受控词表 → 丢弃；两端同名 → 丢弃"""
        text = ('{"entities": [], "relations": ['
                '{"source": "张伟", "target": "李慧敏", "relation": "上级", "strength": 8},'
                '{"source": "张伟", "target": "李慧敏", "relation": "恋爱", "strength": 5},'
                '{"source": "张伟", "target": "张伟", "relation": "上级", "strength": 8}]}')
        parsed = parse_extraction(text)
        assert len(parsed["relations"]) == 1
        assert parsed["relations"][0]["relation"] == "上级"

    def test_markdown_fence_stripped(self):
        text = '```json\n{"entities": [], "relations": []}\n```'
        assert parse_extraction(text) == {"entities": [], "relations": []}

    def test_broken_json_returns_empty(self):
        assert parse_extraction("不是 JSON") == {"entities": [], "relations": []}

    def test_strength_clamped(self):
        text = ('{"entities": [], "relations": ['
                '{"source": "A", "target": "B", "relation": "上级", "strength": 99}]}')
        parsed = parse_extraction(text)
        assert parsed["relations"][0]["strength"] == 10

    def test_single_char_name_dropped(self):
        """批次2-2：单字实体名无图检索价值，丢弃"""
        text = ('{"entities": [{"name": "王", "type": "person", "description": "老板"},'
                ' {"name": "王建国", "type": "person", "description": "总经理"}], "relations": []}')
        parsed = parse_extraction(text)
        assert [e["name"] for e in parsed["entities"]] == ["王建国"]

    def test_noise_name_dropped(self):
        """批次2-2：泛称停用词（"公司"）丢弃；含停用词的复合名（"财务部"）不误伤"""
        text = ('{"entities": [{"name": "公司", "type": "department", "description": "本公司"},'
                ' {"name": "员工", "type": "role", "description": "全体员工"},'
                ' {"name": "财务部", "type": "department", "description": "财务部门"}], "relations": []}')
        parsed = parse_extraction(text)
        assert [e["name"] for e in parsed["entities"]] == ["财务部"]

    def test_example_person_name_dropped(self):
        """批次2-2：制度文本高频示例人名（张三/王五）拦截——入图会让
        "张三和王五是什么关系"误介入产出幻觉级答案"""
        text = ('{"entities": [{"name": "张三", "type": "person", "description": "市场部员工"},'
                ' {"name": "曾敏", "type": "person", "description": "市场专员"}], "relations": []}')
        parsed = parse_extraction(text)
        assert [e["name"] for e in parsed["entities"]] == ["曾敏"]


class TestBuildPrompt:
    def test_prompt_contains_vocab(self):
        p = build_extract_prompt("测试文本")
        assert "person" in p
        assert "上级" in p
        assert "测试文本" in p


class TestFindChain:
    """批次2-3：单实体沿"上级"边定向多跳链（"郑浩的上级的上级是谁"）"""

    def _make_rel(self, relation, start, end):
        class FakeRel:
            def __init__(self, relation, start, end):
                self._data = {"relation": relation}
                self.start_node = {"name": start}
                self.end_node = {"name": end}

            def __getitem__(self, key):
                return self._data[key]

        return FakeRel(relation, start, end)

    def _fake_chain_driver(self, monkeypatch, paths_spec):
        """构造返回多条路径的 fake driver；paths_spec 每条 =
        [(start, end, relation), ...]（边序即路径节点序）"""
        from app.rag.graph import neo4j_store

        def build(spec):
            nodes = [{"name": s} for s, _, _ in spec] + [{"name": spec[-1][1]}]
            rels = [self._make_rel(r, s, e) for s, e, r in spec]
            return type("P", (), {"nodes": nodes, "relationships": rels})()

        paths = [build(spec) for spec in paths_spec]

        class FakeResult:
            """async for 协议：__aiter__ 返回自身（有 __anext__）"""
            def __init__(self, rows):
                self._it = iter(rows)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def run(self, query, **params):
                return FakeResult([{"p": p} for p in paths])

        class FakeDriver:
            def session(self):
                return FakeSession()

        monkeypatch.setattr(neo4j_store, "_get_driver", lambda: FakeDriver())

    @pytest.mark.asyncio
    async def test_chain_rendered(self, monkeypatch):
        from app.rag.graph import neo4j_store

        self._fake_chain_driver(monkeypatch, [
            [("郑浩", "钱进", "上级"), ("钱进", "刘强", "上级"), ("刘强", "王建国", "上级")],
            [("郑浩", "钱进", "上级"), ("钱进", "刘强", "上级")],
        ])
        # 恰好 2 跳优先（"的上级的上级"=期望 2 跳，不返回 3 跳最长链）
        result = await neo4j_store.find_chain("郑浩", hops=2)
        assert result is not None
        assert result["steps"] == ["郑浩的上级是钱进", "钱进的上级是刘强"]
        assert result["length"] == 2

    @pytest.mark.asyncio
    async def test_hops_fallback_to_longest(self, monkeypatch):
        """层级不够（无恰好 hops 链）→ 回退最长链"""
        from app.rag.graph import neo4j_store

        self._fake_chain_driver(monkeypatch, [
            [("郑浩", "钱进", "上级"), ("钱进", "刘强", "上级")],
        ])
        result = await neo4j_store.find_chain("郑浩", hops=3)
        assert result is not None
        assert result["length"] == 2

    @pytest.mark.asyncio
    async def test_no_chain_returns_none(self, monkeypatch):
        from app.rag.graph import neo4j_store

        self._fake_chain_driver(monkeypatch, [])
        assert await neo4j_store.find_chain("王建国") is None


class TestGraphIntent:
    """批次2-3：递归上级/聚合意图检测"""

    def test_recursive_superior(self):
        from app.rag.graph.query import _has_recursive_superior

        assert _has_recursive_superior("郑浩的上级的上级是谁？")
        assert _has_recursive_superior("郑浩的上级的上级的上级是谁？")
        assert not _has_recursive_superior("郑浩的上级是谁？")

    def test_superior_hops(self):
        """跳数推断：重复次数为主，关键词变体兜底（质检#3）"""
        from app.rag.graph.query import _superior_hops

        assert _superior_hops("郑浩的上级的上级是谁？") == 2
        assert _superior_hops("郑浩的上级的上级的上级是谁？") == 3
        assert _superior_hops("刘强的再上一级是谁？") == 2
        assert _superior_hops("郑浩的上两级是谁？") == 2
        assert _superior_hops("郑浩的上级是谁？") == 1

    def test_community_intent(self):
        from app.rag.graph.query import _has_community_intent

        assert _has_community_intent("周涛的团队有哪些人？")
        assert _has_community_intent("张伟的圈子有哪些成员？")
        assert not _has_community_intent("郑浩的上级是谁？")


class TestCommunityDetection:
    """批次2-3：Leiden 分区（真 igraph 跑小图，Neo4j 用 fake driver 供边）"""

    @pytest.mark.asyncio
    async def test_detect_partitions_chain_graph(self, monkeypatch):
        """两簇结构（IT 链 vs 财务链，仅借王建国弱连接）应分出两个社区"""
        from app.rag.graph import community as community_service
        from app.rag.graph import neo4j_store

        edges = [
            # IT 链（强连接簇）
            ("刘强", "钱进", "上级", 8), ("钱进", "郑浩", "上级", 8),
            ("钱进", "彭飞", "上级", 8),
            # 财务链（强连接簇）
            ("赵明远", "王芳", "上级", 8), ("王芳", "周琴", "上级", 8),
            # 弱枢纽连接
            ("王建国", "刘强", "上级", 1), ("王建国", "赵明远", "上级", 1),
        ]

        class FakeResult:
            def __aiter__(self):
                return self

            def __init__(self, rows):
                self._rows = iter(rows)

            async def __anext__(self):
                try:
                    return next(self._rows)
                except StopIteration:
                    raise StopAsyncIteration

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def run(self, query, **params):
                return FakeResult(
                    [{"src": s, "dst": d, "relation": r, "strength": w}
                     for s, d, r, w in edges]
                )

        class FakeDriver:
            def session(self):
                return FakeSession()

        monkeypatch.setattr(neo4j_store, "_get_driver", lambda: FakeDriver())
        partition = await community_service.detect_communities()
        assert set(partition) == {"王建国", "刘强", "钱进", "郑浩", "彭飞",
                                  "赵明远", "王芳", "周琴"}
        # 郑浩/彭飞（IT 链）必须同社区；周琴（财务链）必须不同社区
        assert partition["郑浩"] == partition["彭飞"]
        assert partition["郑浩"] != partition["周琴"]

    @pytest.mark.asyncio
    async def test_empty_graph_returns_empty(self, monkeypatch):
        from app.rag.graph import community as community_service
        from app.rag.graph import neo4j_store

        class FakeResult:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def run(self, query, **params):
                return FakeResult()

        class FakeDriver:
            def session(self):
                return FakeSession()

        monkeypatch.setattr(neo4j_store, "_get_driver", lambda: FakeDriver())
        assert await community_service.detect_communities() == {}


class TestFindPathRender:
    """find_path 方向感知渲染的直接单测（补审高优：此前无直接测试，
    反向穿越 else 分支层级颠倒 bug 得以漏网；语料真值见
    gen_realistic_docs.py 组织架构——"刘强的上级是王建国"）"""

    def _fake_path(self, node_names, edges):
        """构造 fake path 对象（与 neo4j 对象接口一致：节点用 ["name"]、
        关系同时支持 ["relation"] 下标与 .start_node/.end_node 属性访问）：
        edges = [(start_name, end_name, relation), ...]"""

        class FakeRel:
            def __init__(self, relation, start, end):
                self._data = {"relation": relation}
                self.start_node = {"name": start}
                self.end_node = {"name": end}

            def __getitem__(self, key):
                return self._data[key]

        nodes = [{"name": n} for n in node_names]
        rels = [
            FakeRel(relation, start, end)
            for start, end, relation in edges
        ]
        return type("P", (), {"nodes": nodes, "relationships": rels})()

    @pytest.mark.asyncio
    async def test_reverse_traversal_renders_correct_semantics(self, monkeypatch):
        """反向问法：王建国→郑浩 的路径段[王建国,刘强] 配边(刘强)-[上级]->(王建国)
        必须渲染"王建国是刘强的上级"（而非颠倒的"刘强是王建国的上级"）"""
        from app.rag.graph import neo4j_store

        fake_path = self._fake_path(
            ["王建国", "刘强"],
            [("刘强", "王建国", "上级")],
        )

        class FakeResult:
            async def single(self):
                return {"p": fake_path}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def run(self, query, **params):
                return FakeResult()

        class FakeDriver:
            def session(self):
                return FakeSession()

        monkeypatch.setattr(neo4j_store, "_get_driver", lambda: FakeDriver())
        result = await neo4j_store.find_path("王建国", "刘强")
        assert result is not None
        assert result["steps"] == ["王建国是刘强的上级"]

    @pytest.mark.asyncio
    async def test_forward_traversal_unchanged(self, monkeypatch):
        """正向问法：郑浩→钱进 配边(郑浩)-[上级]->(钱进) 渲染"郑浩的上级是钱进"（不回归）"""
        from app.rag.graph import neo4j_store

        fake_path = self._fake_path(
            ["郑浩", "钱进"],
            [("郑浩", "钱进", "上级")],
        )

        class FakeResult:
            async def single(self):
                return {"p": fake_path}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def run(self, query, **params):
                return FakeResult()

        class FakeDriver:
            def session(self):
                return FakeSession()

        monkeypatch.setattr(neo4j_store, "_get_driver", lambda: FakeDriver())
        result = await neo4j_store.find_path("郑浩", "钱进")
        assert result is not None
        assert result["steps"] == ["郑浩的上级是钱进"]


class TestNeighborIntent:
    """批次2-2 补强：邻居按问句意图过滤方向——"下属"只留下级边、"上级"只留上级边"""

    ROWS = [
        {"name": "刘强", "relation": "上级", "strength": 8, "direction": "out"},  # 对方是本实体的上级
        {"name": "彭飞", "relation": "上级", "strength": 8, "direction": "in"},   # 本实体是对方的上级
        {"name": "IT部", "relation": "负责", "strength": 5, "direction": "out"},  # 负责边
        {"name": "郑浩", "relation": "下属", "strength": 7, "direction": "out"},  # 本实体-[下属]->对方
    ]

    def test_subordinates_keeps_only_downward_edges(self):
        from app.rag.graph.query import _filter_neighbors_by_intent

        rows = _filter_neighbors_by_intent(self.ROWS, "subordinates")
        assert [r["name"] for r in rows] == ["彭飞", "郑浩"]

    def test_superior_keeps_only_upward_edges(self):
        from app.rag.graph.query import _filter_neighbors_by_intent

        rows = _filter_neighbors_by_intent(self.ROWS, "superior")
        assert [r["name"] for r in rows] == ["刘强"]

    def test_none_intent_keeps_all(self):
        from app.rag.graph.query import _filter_neighbors_by_intent

        assert len(_filter_neighbors_by_intent(self.ROWS, None)) == 4

    def test_intent_detection(self):
        from app.rag.graph.query import _neighbor_intent

        assert _neighbor_intent("钱进的下属有哪些？") == "subordinates"
        assert _neighbor_intent("郑浩的上级是谁？") == "superior"
        assert _neighbor_intent("郑浩和钱进是什么关系？") is None


class TestFormatContext:
    def test_path_chain_rendered(self):
        result = {"entities": ["郑浩", "王建国"],
                  "path": {"steps": ["郑浩的上级是钱进", "钱进的上级是王建国"], "length": 2},
                  "neighbors": []}
        text = format_context(result)
        assert "郑浩的上级是钱进" in text
        assert "钱进的上级是王建国" in text
        assert "关系链（2 跳）" in text
        assert "[KG]" in text

    def test_neighbors_rendered(self):
        """邻居方向正确渲染：入边（刘强-上级->钱进）= 本实体（钱进）是对方（刘强）的下级"""
        result = {"entities": ["钱进"],
                  "path": None,
                  "neighbors": [{"name": "刘强", "relation": "上级", "strength": 8, "direction": "in"}]}
        text = format_context(result)
        assert "刘强（本实体是对方的上级，强度 8）" in text


class TestNeo4jStoreFakeDriver:
    @pytest.mark.asyncio
    async def test_is_available_without_password(self, monkeypatch):
        """未配置密码 → 不可用（真实环境默认关闭图谱，降级纯向量）"""
        from app.rag.graph import neo4j_store

        monkeypatch.setattr("app.config.settings.neo4j_password", "")
        neo4j_store._driver = None
        assert await neo4j_store.is_available() is False


class TestEnvIsolation:
    """批次2-2 方案C：节点 MERGE 键与全部写查询带 env 租户标签"""

    @pytest.mark.asyncio
    async def test_upsert_passes_env_to_merge(self, monkeypatch):
        from app.rag.graph import neo4j_store

        calls: list[tuple[str, dict]] = []

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def run(self, query, **params):
                calls.append((query, params))

        class FakeDriver:
            def session(self):
                return FakeSession()

        monkeypatch.setattr(neo4j_store, "_get_driver", lambda: FakeDriver())
        monkeypatch.setattr("app.config.settings.neo4j_env", "demo")
        await neo4j_store.upsert_document_graph(
            "d1", "f.txt",
            [{"name": "王建国", "type": "person", "description": "总经理"}],
            [{"source": "郑浩", "target": "钱进", "relation": "上级", "strength": 8}],
        )
        merges = [c for c in calls if "MERGE" in c[0]]
        # 三条 MERGE run：实体节点 / 关系端点（一条语句三个 MERGE）/ 建图标记节点
        assert len(merges) >= 3, "应有实体/端点/标记节点的 MERGE 调用"
        for query, params in merges:
            assert params.get("env") == "demo", f"MERGE 缺 env 参数: {query}"
        # 节点匹配键必须含 env（否则两环境同名实体互串）
        assert "{name: $name, env: $env}" in merges[0][0]
        assert "{name: $src, env: $env}" in merges[1][0]

    @pytest.mark.asyncio
    async def test_entity_queries_filter_by_env(self, monkeypatch):
        from app.rag.graph import neo4j_store

        calls: list[tuple[str, dict]] = []

        class FakeResult:
            """支持 async 迭代的空结果（模拟无数据返回）"""

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def run(self, query, **params):
                calls.append((query, params))
                return FakeResult()

        class FakeDriver:
            def session(self):
                return FakeSession()

        monkeypatch.setattr(neo4j_store, "_get_driver", lambda: FakeDriver())
        monkeypatch.setattr("app.config.settings.neo4j_env", "demo")
        await neo4j_store.all_entity_names()
        await neo4j_store.neighbors("钱进")
        await neo4j_store.entity_details()
        assert calls, "应有查询调用"
        for query, params in calls:
            assert params.get("env") == "demo", f"查询缺 env 参数: {query}"


class TestDisambiguate:
    """批次2-2 实体消歧：称呼 → 规范节点名（LLM 确认 + 幻觉名过滤）"""

    @pytest.mark.asyncio
    async def test_maps_alias_to_node(self, monkeypatch):
        from app.rag import llm as llm_module
        from app.rag.graph import query as graph_query

        async def fake_details():
            return [
                {"name": "王建国", "type": "person", "description": "总经理"},
                {"name": "刘强", "type": "person", "description": "IT 总监"},
            ]

        class FakeLLM:
            def invoke(self, prompt):
                return type("Resp", (), {"content": '{"mapping": {"王总": "王建国"}}'})()

        monkeypatch.setattr(graph_query.neo4j_store, "entity_details", fake_details)
        monkeypatch.setattr(llm_module, "get_llm", lambda **kw: FakeLLM())
        mapping = await graph_query._disambiguate(["王总"], "王总的直接下属有哪些？")
        assert mapping == {"王总": "王建国"}

    @pytest.mark.asyncio
    async def test_hallucinated_name_rejected(self, monkeypatch):
        """LLM 映射到候选池外的名字 → 置 None（防幻觉名进查询）"""
        from app.rag import llm as llm_module
        from app.rag.graph import query as graph_query

        async def fake_details():
            return [{"name": "王建国", "type": "person", "description": "总经理"}]

        class FakeLLM:
            def invoke(self, prompt):
                return type("Resp", (), {
                    "content": '{"mapping": {"王总": "不存在的董事", "刘总": null}}'
                })()

        monkeypatch.setattr(graph_query.neo4j_store, "entity_details", fake_details)
        monkeypatch.setattr(llm_module, "get_llm", lambda **kw: FakeLLM())
        mapping = await graph_query._disambiguate(["王总", "刘总"], "王总和刘总的下属")
        assert mapping == {"王总": None, "刘总": None}

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty(self, monkeypatch):
        from app.rag import llm as llm_module
        from app.rag.graph import query as graph_query

        async def fake_details():
            return [{"name": "王建国", "type": "person", "description": "总经理"}]

        class BrokenLLM:
            def invoke(self, prompt):
                raise RuntimeError("boom")

        monkeypatch.setattr(graph_query.neo4j_store, "entity_details", fake_details)
        monkeypatch.setattr(llm_module, "get_llm", lambda **kw: BrokenLLM())
        assert await graph_query._disambiguate(["王总"], "王总的直接下属有哪些？") == {}


@pytest.fixture(autouse=True)
def _clear_community_cache():
    """批次3 质检建议6：模块级 _COMMUNITY_CACHE 是跨用例共享状态，
    每个用例前清理防缓存污染（同问句命中上个用例写入的旧结果）"""
    from app.rag.graph import query as graph_query

    graph_query._COMMUNITY_CACHE.clear()
    yield


class TestCommunityLevelQuery:
    """批次2-5：社区级问答（不点名问法）与审批人实体反查"""

    @pytest.mark.asyncio
    async def test_match_community_by_question(self, monkeypatch):
        """embedding 召回 top3 + LLM 确认（LLM 选候选中的社区）→ 返回社区"""
        from app.rag import llm as llm_module
        from app.rag.graph import neo4j_store
        from app.rag.graph import query as graph_query

        async def fake_communities():
            return [
                {"id": 2, "summary": "研发团队，负责产品研发",
                 "members": ["周涛", "吴磊", "冯刚"], "count": 3},
                {"id": 3, "summary": "IT 运维团队",
                 "members": ["刘强", "钱进", "郑浩"], "count": 3},
                {"id": 0, "summary": "泛称部门簇",
                 "members": ["人事部"], "count": 1},
            ]

        class FakeEmbeddings:
            def embed_query(self, q):
                return [1.0, 0.0, 0.0]

            def embed_documents(self, texts):
                return [[1.0, 0.0, 0.0] if "研发" in t else [0.0, 1.0, 0.0]
                        for t in texts]

        class FakeLLM:
            def invoke(self, prompt):
                # 候选列表展示编号：社区3 排第一、社区4 排第二…
                return type("Resp", (), {"content": '{"community": 3}'})()

        monkeypatch.setattr(neo4j_store, "all_communities", fake_communities)
        monkeypatch.setattr(
            "app.rag.embeddings.get_embeddings", lambda: FakeEmbeddings()
        )
        monkeypatch.setattr(llm_module, "get_llm", lambda **kw: FakeLLM())
        comm = await graph_query._match_community_by_question("研发团队有哪些人？")
        assert comm is not None
        assert comm["id"] == 2  # 展示编号 3 = 社区 id 2

    @pytest.mark.asyncio
    async def test_match_community_llm_null_returns_none(self, monkeypatch):
        """排序差距小（<0.15）走 LLM 确认，LLM 判不指代 → None 回退向量"""
        from app.rag import llm as llm_module
        from app.rag.graph import neo4j_store
        from app.rag.graph import query as graph_query

        async def fake_communities():
            return [
                {"id": 0, "summary": "行政圈", "members": ["陈静"], "count": 1},
                {"id": 1, "summary": "财务圈", "members": ["赵明远"], "count": 1},
                {"id": 2, "summary": "研发圈", "members": ["周涛"], "count": 1},
            ]

        class FakeEmbeddings:
            def embed_query(self, q):
                return [1.0, 0.0, 0.0]

            def embed_documents(self, texts):
                # 相似度 0.9/0.85/0.8——top1-top2 差距 0.05 < 0.15 → 走 LLM
                sims = [0.9, 0.85, 0.8]
                return [[s, 0.0, 0.0] for s in sims]

        class FakeLLM:
            def invoke(self, prompt):
                return type("Resp", (), {"content": '{"community": null}'})()

        monkeypatch.setattr(neo4j_store, "all_communities", fake_communities)
        monkeypatch.setattr(
            "app.rag.embeddings.get_embeddings", lambda: FakeEmbeddings()
        )
        monkeypatch.setattr(llm_module, "get_llm", lambda **kw: FakeLLM())
        assert await graph_query._match_community_by_question("市场团队有哪些人？") is None

    @pytest.mark.asyncio
    async def test_confident_gap_skips_llm(self, monkeypatch):
        """批次3 高置信快速路：top1-top2 差距 > 0.15 → 直接取 top1（零 LLM 调用）"""
        from app.rag import llm as llm_module
        from app.rag.graph import neo4j_store
        from app.rag.graph import query as graph_query

        async def fake_communities():
            return [
                {"id": 0, "summary": "行政圈", "members": ["陈静"], "count": 1},
                {"id": 1, "summary": "研发圈", "members": ["周涛"], "count": 1},
            ]

        class FakeEmbeddings:
            def embed_query(self, q):
                return [1.0, 0.0, 0.0]

            def embed_documents(self, texts):
                # 相似度 0.9 / 0.5——差距 0.4 > 0.15 → 快速路
                return [[0.9, 0.0, 0.0], [0.5, 0.5, 0.0]]

        llm_called = {"n": 0}

        class FakeLLM:
            def invoke(self, prompt):
                llm_called["n"] += 1
                return type("Resp", (), {"content": '{"community": null}'})()

        monkeypatch.setattr(neo4j_store, "all_communities", fake_communities)
        monkeypatch.setattr(
            "app.rag.embeddings.get_embeddings", lambda: FakeEmbeddings()
        )
        monkeypatch.setattr(llm_module, "get_llm", lambda **kw: FakeLLM())
        comm = await graph_query._match_community_by_question("研发团队有哪些人？")
        assert comm is not None
        assert comm["id"] == 0  # top1 社区（相似度 0.9 的行政圈排第一）
        assert llm_called["n"] == 0  # 快速路未调 LLM

    @pytest.mark.asyncio
    async def test_match_community_below_threshold_skips_llm(self, monkeypatch):
        """top1 相似度低于硬门槛 → 不喂 LLM 直接回退（防无关问题硬匹配）"""
        from app.rag import llm as llm_module
        from app.rag.graph import neo4j_store
        from app.rag.graph import query as graph_query

        async def fake_communities():
            return [{"id": 0, "summary": "行政圈", "members": ["陈静"], "count": 1}]

        class FakeEmbeddings:
            def embed_query(self, q):
                return [1.0, 0.0, 0.0]

            def embed_documents(self, texts):
                return [[0.0, 1.0, 0.0] for _ in texts]  # 正交=相似度 0

        llm_called = {"n": 0}

        class FakeLLM:
            def invoke(self, prompt):
                llm_called["n"] += 1
                return type("Resp", (), {"content": '{"community": 0}'})()

        monkeypatch.setattr(neo4j_store, "all_communities", fake_communities)
        monkeypatch.setattr(
            "app.rag.embeddings.get_embeddings", lambda: FakeEmbeddings()
        )
        monkeypatch.setattr(llm_module, "get_llm", lambda **kw: FakeLLM())
        assert await graph_query._match_community_by_question("年会奖品有哪些？") is None
        assert llm_called["n"] == 0

    @pytest.mark.asyncio
    async def test_entity_context_by_description(self, monkeypatch):
        """审批人"财务总监" → 描述匹配实体赵明远 → 返回其邻居"""
        from app.rag.graph import neo4j_store
        from app.rag.graph import query as graph_query

        async def fake_names():
            return ["赵明远", "王建国"]

        async def fake_details():
            return [
                {"name": "赵明远", "type": "person", "description": "财务总监"},
                {"name": "王建国", "type": "person", "description": "总经理"},
            ]

        async def fake_neighbors(entity, limit=10):
            if entity == "赵明远":
                return [{"name": "王建国", "relation": "上级", "strength": 8,
                         "direction": "out"}]
            return []

        monkeypatch.setattr(neo4j_store, "all_entity_names", fake_names)
        monkeypatch.setattr(neo4j_store, "entity_details", fake_details)
        monkeypatch.setattr(neo4j_store, "neighbors", fake_neighbors)
        ctx = await graph_query.entity_context_for_text("财务总监")
        assert ctx is not None
        assert ctx["entity"] == "赵明远"
        assert ctx["neighbors"][0]["name"] == "王建国"

    @pytest.mark.asyncio
    async def test_entity_context_no_match_returns_none(self, monkeypatch):
        """泛称"直属上级"无对应实体 → None（静默跳过不挂图谱）"""
        from app.rag.graph import neo4j_store
        from app.rag.graph import query as graph_query

        async def fake_names():
            return ["赵明远"]

        async def fake_details():
            return [{"name": "赵明远", "type": "person", "description": "财务总监"}]

        monkeypatch.setattr(neo4j_store, "all_entity_names", fake_names)
        monkeypatch.setattr(neo4j_store, "entity_details", fake_details)
        assert await graph_query.entity_context_for_text("直属上级") is None


class TestRelationEval:
    """批次2-2 关系层评估：图谱链路分环节打分（不借用文档命中口径）

    质检 G1 修复：fake_search 必须反映真实路由——单实体题返回邻居
    （path 为 None），双实体题才返回 path，否则评估指标与链路脱节。
    """

    @pytest.mark.asyncio
    async def test_metrics_computed(self, monkeypatch):
        from app.rag.graph import neo4j_store
        from app.rag.graph import query as graph_query
        from app.services import eval_service

        async def fake_available():
            return True

        async def fake_search(question, top_k=6):
            if "张三" in question:
                return None
            if "是什么关系" in question and "郑浩" in question and "刘强" in question:
                return {
                    "entities": ["郑浩", "刘强"],
                    "path": {"steps": ["郑浩的上级是钱进", "钱进的上级是刘强"], "length": 2},
                    "neighbors": [],
                }
            # 单实体题走邻居（真实路由）
            return {
                "entities": ["郑浩"],
                "path": None,
                "neighbors": [
                    {"name": "钱进", "relation": "上级", "strength": 8, "direction": "out"},
                ],
            }

        monkeypatch.setattr(neo4j_store, "is_available", fake_available)
        monkeypatch.setattr(graph_query, "search", fake_search)
        queries = [
            {"question": "郑浩的上级是谁？", "kind": "neighbors",
             "expected_entities": ["郑浩"], "expected_neighbors": ["钱进"]},
            {"question": "郑浩和刘强是什么关系？", "kind": "path",
             "expected_entities": ["郑浩", "刘强"],
             "expected_path": ["郑浩的上级是钱进", "钱进的上级是刘强"]},
            {"question": "张三和王五是什么关系？", "kind": "no_intervention"},
        ]
        r = await eval_service._run_relation_group_async(queries)
        assert r["n"] == 3
        assert r["graph_available"] is True
        assert r["intervention_rate"] == 2 / 3
        assert r["entity_link_accuracy"] == 1.0
        assert r["path_accuracy"] == 1.0
        assert r["neighbor_accuracy"] == 1.0
        assert r["no_intervention_accuracy"] == 1.0

    @pytest.mark.asyncio
    async def test_false_intervention_penalized(self, monkeypatch):
        """质检 G2：应不介入的题被图谱误介入 → 不介入正确率扣分"""
        from app.rag.graph import neo4j_store
        from app.rag.graph import query as graph_query
        from app.services import eval_service

        async def fake_available():
            return True

        async def fake_search(question, top_k=6):
            # 无论什么题都介入（模拟误介入）
            return {
                "entities": ["郑浩"],
                "path": None,
                "neighbors": [{"name": "钱进", "relation": "上级", "strength": 8, "direction": "out"}],
            }

        monkeypatch.setattr(neo4j_store, "is_available", fake_available)
        monkeypatch.setattr(graph_query, "search", fake_search)
        r = await eval_service._run_relation_group_async(
            [{"question": "张三和王五是什么关系？", "kind": "no_intervention"}]
        )
        assert r["no_intervention_accuracy"] == 0.0

    @pytest.mark.asyncio
    async def test_community_metrics_computed(self, monkeypatch):
        """批次2-3：社区题打分——期望成员是实际社区成员的子集"""
        from app.rag.graph import neo4j_store
        from app.rag.graph import query as graph_query
        from app.services import eval_service

        async def fake_available():
            return True

        async def fake_search(question, top_k=6):
            return {
                "entities": ["周涛"],
                "path": None,
                "neighbors": [],
                "community": {
                    "id": 2,
                    "summary": "研发团队",
                    "members": ["周涛", "吴磊", "冯刚", "戴军", "肖燕", "研发一部"],
                    "count": 6,
                },
            }

        monkeypatch.setattr(neo4j_store, "is_available", fake_available)
        monkeypatch.setattr(graph_query, "search", fake_search)
        r = await eval_service._run_relation_group_async(
            [{"question": "周涛的团队有哪些人？", "kind": "community",
              "expected_entities": ["周涛"],
              "expected_members": ["吴磊", "冯刚", "戴军", "肖燕"]}]
        )
        assert r["community_accuracy"] == 1.0
        assert r["intervention_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_graph_unavailable_marks_false(self, monkeypatch):
        from app.rag.graph import neo4j_store
        from app.services import eval_service

        async def fake_available():
            return False

        monkeypatch.setattr(neo4j_store, "is_available", fake_available)
        r = await eval_service._run_relation_group_async(
            [{"question": "郑浩的上级是谁？", "kind": "path",
              "expected_entities": ["郑浩"], "expected_path": ["郑浩的上级是钱进"]}]
        )
        assert r["graph_available"] is False
        assert r["intervention_rate"] == 0.0
