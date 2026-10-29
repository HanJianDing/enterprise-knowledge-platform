/** 检索调优页（验收批次1·1a）：同义词表 + 词权重
 *
 * BM25 腿的提准配置，保存后即时生效（后端带 mtime 缓存）。
 * - 同义词表：查询词 → 文档中的同义变体（礼品→礼金、报销→费用报销），
 *   解决"问法不同导致词面漏配"（礼品题回归案例）
 * - 词权重：重要词加权（>1 提升匹配贡献，BM25 查询词按权重重复）
 */
import React, { useEffect, useState } from "react";
import {
  Card, Button, Space, Input, InputNumber, Select, Tag, Typography, Alert,
} from "antd";
import { PlusOutlined, DeleteOutlined, SaveOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import { adminApi } from "../../services/adminApi";

const { Text } = Typography;

interface SynonymRow {
  uid: number; // 8e 修复：稳定行标识（数组索引当 key 增删行时 React 状态会错位）
  term: string;
  synonyms: string[];
}

interface WeightRow {
  uid: number;
  term: string;
  weight: number;
}

// 行 uid 自增计数器（组件内唯一即可）
let _rowUid = 0;
const _nextUid = () => ++_rowUid;

const RetrievalTuningPage: React.FC = () => {
  const [synRows, setSynRows] = useState<SynonymRow[]>([]);
  const [weightRows, setWeightRows] = useState<WeightRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  // 8g：同义词自动发现（LLM 建议；采纳=后端立即合并保存生效并留审计）
  const [suggestLoading, setSuggestLoading] = useState(false);
  const [suggestions, setSuggestions] = useState<
    { query_term: string; doc_term: string; reason: string }[]
  >([]);
  const [suggestMsg, setSuggestMsg] = useState("");

  const handleSuggest = async () => {
    setSuggestLoading(true);
    setSuggestMsg("");
    try {
      const r = await adminApi.suggestRetrievalTerms();
      // 质检中危修复：扫描结果已入后端暂存，统一重新拉取待审列表——
      // 不再用"本次新增"覆盖列表（无新增时旧待审项会从 UI 消失）
      const pending = await adminApi.getRetrievalSuggestions();
      setSuggestions(pending.suggestions);
      setSuggestMsg(pending.suggestions.length ? r.message : `${r.message}（可多使用系统后再试）`);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || "建议生成失败");
    } finally {
      setSuggestLoading(false);
    }
  };

  const handleAdopt = async (s: { query_term: string; doc_term: string }) => {
    // 8g 自动化：采纳即保存生效（后端合并配置+落盘+审计），不再手动点保存
    try {
      const r = await adminApi.adoptRetrievalTerm(s.query_term, s.doc_term);
      message.success(r.message);
      // 同步本地表单状态，保证页面展示与实际配置一致
      setSynRows((prev) => {
        const found = prev.find((row) => row.term === s.query_term);
        if (found) {
          return prev.map((row) =>
            row.uid === found.uid && !row.synonyms.includes(s.doc_term)
              ? { ...row, synonyms: [...row.synonyms, s.doc_term] }
              : row
          );
        }
        return [...prev, { uid: _nextUid(), term: s.query_term, synonyms: [s.doc_term] }];
      });
      setSuggestions((prev) => prev.filter((sug) => sug !== s));
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || "采纳失败");
    }
  };

  const handleIgnore = async (s: { query_term: string; doc_term: string }) => {
    try {
      await adminApi.ignoreRetrievalTerm(s.query_term, s.doc_term);
      setSuggestions((prev) => prev.filter((sug) => sug !== s));
    } catch {
      message.error("操作失败");
    }
  };

  useEffect(() => {
    adminApi.getRetrievalTerms()
      .then((r) => {
        setSynRows(Object.entries(r.synonyms).map(([term, synonyms]) => ({ uid: _nextUid(), term, synonyms })));
        setWeightRows(Object.entries(r.weights).map(([term, weight]) => ({ uid: _nextUid(), term, weight })));
      })
      .catch(() => message.error("加载检索配置失败"))
      .finally(() => setLoaded(true));
    // 8g：加载待审建议（后台定时扫描 + 手动扫描的结果）
    adminApi.getRetrievalSuggestions()
      .then((r) => setSuggestions(r.suggestions))
      .catch(() => {});
  }, []);

  const handleSave = async () => {
    const synonyms: Record<string, string[]> = {};
    for (const row of synRows) {
      const term = row.term.trim();
      const syns = row.synonyms.map((s) => s.trim()).filter(Boolean);
      if (!term) continue;
      synonyms[term] = syns.length ? syns : synonyms[term] || [];
    }
    const weights: Record<string, number> = {};
    for (const row of weightRows) {
      const term = row.term.trim();
      if (term && row.weight > 0) weights[term] = row.weight;
    }
    setSaving(true);
    try {
      await adminApi.saveRetrievalTerms({ synonyms, weights });
      message.success("已保存，检索即时生效");
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ padding: 24 }}>
      <PageHeader title="检索调优" description="BM25 腿的提准配置：同义词表 + 词权重（保存后即时生效）" />
      <Alert
        type="info" showIcon style={{ marginBottom: 16 }}
        message="同义词表解决'问法不同、词面漏配'：如员工问'礼品'、制度写'礼金'。词权重 >1 加码重要词、<1 给通用词降噪（如 多少/什么=0.5）。"
      />
      <Card title="智能建议（8g：LLM 扫描近期提问，自动发现问法错配）" style={{ marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: "100%" }}>
          <Button
            icon={<ThunderboltOutlined />}
            loading={suggestLoading}
            onClick={handleSuggest}
          >
            扫描近期提问生成建议
          </Button>
          {suggestMsg && <Text type="secondary">{suggestMsg}</Text>}
          {suggestions.map((s, i) => (
            <Space key={i} wrap>
              <Tag color="blue">{s.query_term}</Tag>
              <span>→</span>
              <Tag color="green">{s.doc_term}</Tag>
              <Text type="secondary">{s.reason}</Text>
              <Button
                size="small" type="primary"
                onClick={() => handleAdopt(s)}
              >
                采纳并生效
              </Button>
              <Button size="small" onClick={() => handleIgnore(s)}>
                忽略
              </Button>
            </Space>
          ))}
        </Space>
      </Card>
      <Card title="同义词表（查询词 → 文档中的同义变体）" style={{ marginBottom: 16 }} loading={!loaded}>
        <Space direction="vertical" style={{ width: "100%" }} size={8}>
          {synRows.map((row) => (
            <Space.Compact key={row.uid} style={{ width: "100%" }}>
              <Input
                style={{ width: 180 }}
                placeholder="查询词（如 礼品）"
                value={row.term}
                onChange={(e) =>
                  setSynRows((prev) => prev.map((r) => r.uid === row.uid ? { ...r, term: e.target.value } : r))
                }
              />
              <Select
                mode="tags"
                style={{ flex: 1 }}
                placeholder="文档中的同义变体（如 礼金），回车添加"
                value={row.synonyms}
                onChange={(vals: string[]) =>
                  setSynRows((prev) => prev.map((r) => r.uid === row.uid ? { ...r, synonyms: vals } : r))
                }
                open={false}
                suffixIcon={null}
                tokenSeparators={[","]}
              />
              <Button
                icon={<DeleteOutlined />}
                onClick={() => setSynRows((prev) => prev.filter((r) => r.uid !== row.uid))}
              />
            </Space.Compact>
          ))}
          <Button
            type="dashed" block icon={<PlusOutlined />}
            onClick={() => setSynRows((prev) => [...prev, { uid: _nextUid(), term: "", synonyms: [] }])}
          >
            添加同义词
          </Button>
        </Space>
      </Card>
      <Card title="词权重（>1 加码 / <1 降噪，1.0 = 不加码）" style={{ marginBottom: 16 }} loading={!loaded}>
        <Space direction="vertical" style={{ width: "100%" }} size={8}>
          {weightRows.map((row) => (
            <Space.Compact key={row.uid} style={{ width: "100%" }}>
              <Input
                style={{ width: 220 }}
                placeholder="加权词（如 考勤）"
                value={row.term}
                onChange={(e) =>
                  setWeightRows((prev) => prev.map((r) => r.uid === row.uid ? { ...r, term: e.target.value } : r))
                }
              />
              <InputNumber
                min={0.1} max={10} step={0.1}
                value={row.weight}
                onChange={(v) =>
                  setWeightRows((prev) => prev.map((r) => r.uid === row.uid ? { ...r, weight: v ?? 1.0 } : r))
                }
              />
              <Button
                icon={<DeleteOutlined />}
                onClick={() => setWeightRows((prev) => prev.filter((r) => r.uid !== row.uid))}
              />
            </Space.Compact>
          ))}
          <Button
            type="dashed" block icon={<PlusOutlined />}
            onClick={() => setWeightRows((prev) => [...prev, { uid: _nextUid(), term: "", weight: 1.0 }])}
          >
            添加加权词
          </Button>
        </Space>
      </Card>
      <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
        保存配置
      </Button>
      <Text type="secondary" style={{ marginLeft: 12 }}>
        同义词与权重均只作用于关键词（BM25）检索腿；语义向量腿不受影响。
      </Text>
    </div>
  );
};

export default RetrievalTuningPage;
