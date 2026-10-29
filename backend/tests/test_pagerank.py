"""验收批次1·1b 测试：文档引用图挖掘与 PageRank 幂迭代（纯函数部分）"""
from app.rag.pagerank import _mine_edges, _power_iterate, _EXCLUDED_SOURCES


def _chunk(doc_id: str, filename: str, content: str) -> dict:
    return {"document_id": doc_id, "filename": filename, "content": content}


class TestMineEdges:
    def test_reference_by_title(self):
        """A 的内容提到 B 的标题 → 边 A→B（document_id 键）"""
        chunks = [
            _chunk("A", "新员工入职培训教材.md", "新员工培训见培训管理制度。"),
            _chunk("B", "培训管理制度.txt", "公司每年为每位员工提供不少于40学时的培训。"),
            _chunk("C", "考勤补卡管理制度.txt", "每日打卡，详见考勤管理制度。"),
            _chunk("D", "考勤管理制度.txt", "每日打卡规定。"),
        ]
        edges = _mine_edges(chunks)
        assert "B" in edges.get("A", set())  # 教材 → 培训管理制度
        assert "D" in edges.get("C", set())  # 补卡制度 → 考勤管理制度
        # 不引用自身
        for src, targets in edges.items():
            assert src not in targets

    def test_compilation_sources_excluded(self):
        """汇编型文档（目录全量拼接）不参与引用投票"""
        chunks = [
            _chunk("asm", "管理制度汇编.txt", "目录\n1. 培训管理制度\n2. 考勤管理制度"),
            _chunk("B", "培训管理制度.txt", "培训内容。"),
            _chunk("C", "考勤管理制度.txt", "考勤内容。"),
        ]
        edges = _mine_edges(chunks)
        assert "asm" not in edges  # 汇编不作为源

    def test_short_titles_ignored(self):
        """标题 <4 字不参与匹配（防短词噪声）"""
        chunks = [
            _chunk("A", "培训管理制度.txt", "本制度由人事部解释。"),
            _chunk("B", "OA.txt", "使用 OA 系统。"),
        ]
        edges = _mine_edges(chunks)
        # "OA" 短词即便出现在 A 内容中也不建边
        assert all("B" not in t for t in edges.values())


class TestPowerIterate:
    def test_referenced_doc_scores_higher(self):
        """被引用文档的 pagerank 高于孤立文档"""
        edges = {"A": {"B"}}
        pr = _power_iterate(edges, ["A", "B", "C"])
        assert pr["B"] > pr["C"]
        assert pr["B"] > pr["A"]

    def test_normalized_max_one(self):
        """归一化：最大值为 1"""
        pr = _power_iterate({"A": {"B"}, "B": {"C"}, "C": {"A"}}, ["A", "B", "C", "D"])
        assert max(pr.values()) == 1.0

    def test_empty_graph(self):
        assert _power_iterate({}, []) == {}
