import React, { useState } from "react";
import {
  Card, Input, Button, Select, Switch, Space, Spin, Tabs, Table, Tag,
  Statistic, Row, Col, Empty, Alert, Typography,
} from "antd";

const { Text } = Typography;
import { SearchOutlined, ThunderboltOutlined, ExperimentOutlined, CheckCircleOutlined, CloseCircleOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import {
  adminApi,
  type RetrievalTestResult,
  type RetrievalTestDoc,
  type EvalRunResult,
  type EvalRelationRunResult,
  type EvalApprovalRunResult,
  type EvalGroupResult,
} from "../../services/adminApi";

/** 试检索页：输入问题即时查看检索命中（调参即时反馈） */
const RetrievalTestTab: React.FC = () => {
  const [question, setQuestion] = useState("");
  const [topK, setTopK] = useState(6);
  const [withRerank, setWithRerank] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<RetrievalTestResult | null>(null);

  const handleSearch = async () => {
    if (!question.trim()) {
      message.warning("请输入检索问题");
      return;
    }
    setLoading(true);
    try {
      setResult(await adminApi.evalRetrievalTest(question.trim(), topK, withRerank));
    } catch (err: any) {
      message.error(err.response?.data?.detail || "试检索失败");
    } finally {
      setLoading(false);
    }
  };

  const columns: ColumnsType<RetrievalTestDoc> = [
    { title: "#", dataIndex: "index", key: "index", width: 50 },
    { title: "文档", dataIndex: "filename", key: "filename", width: 200, render: (n: string) => <Tag color="blue">{n}</Tag> },
    { title: "切片号", dataIndex: "chunk_index", key: "ci", width: 80, render: (n: number) => `第 ${n + 1} 块` },
    {
      title: "内容预览", dataIndex: "content_preview", key: "content",
      render: (_t: string, record) => (
        <div style={{ whiteSpace: "pre-wrap", fontSize: 13, lineHeight: 1.7 }}>
          {record.content_preview}
          {record.content.length > 300 && "…"}
        </div>
      ),
    },
  ];

  return (
    <Card>
      <Space wrap style={{ marginBottom: 16 }}>
        <Input.Search
          placeholder="输入问题，如：年假怎么申请"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onSearch={handleSearch}
          enterButton={<><SearchOutlined /> 试检索</>}
          style={{ width: 360 }}
          loading={loading}
          allowClear
        />
        <span>返回条数</span>
        <Select value={topK} onChange={setTopK} style={{ width: 80 }}
          options={[3, 6, 10, 20].map((k) => ({ label: String(k), value: k }))}
        />
        <span>rerank 精排</span>
        <Switch checked={withRerank} onChange={setWithRerank} />
        <Button icon={<ThunderboltOutlined />} onClick={handleSearch} loading={loading}>
          重新检索
        </Button>
      </Space>
      <Spin spinning={loading}>
        {result === null ? (
          <Empty description="输入问题后查看混合检索命中的切片（管理端调参可视化）" />
        ) : result.docs.length === 0 ? (
          <Empty description="未命中任何切片" />
        ) : (
          <Table
            columns={columns}
            dataSource={result.docs}
            rowKey={(r) => `${r.filename}-${r.chunk_index}`}
            pagination={false}
            size="small"
          />
        )}
      </Spin>
    </Card>
  );
};

/** 评估跑分页：分层评估集一键跑（验收批次1：all/回归层/语料层），展示检索质量指标（面试现场武器） */
const EvalRunTab: React.FC = () => {
  const [withRerank, setWithRerank] = useState(false);
  const [evalSet, setEvalSet] = useState("all");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<EvalRunResult | null>(null);

  const handleRun = async () => {
    setLoading(true);
    try {
      setResult(await adminApi.runEval(withRerank, evalSet));
      message.success("评估完成");
    } catch (err: any) {
      message.error(err.response?.data?.detail || "评估失败");
    } finally {
      setLoading(false);
    }
  };

  const groupColumns: ColumnsType<EvalGroupResult["details"][number]> = [
    { title: "结果", key: "hit", width: 70, render: (_, r) => r.hit
      ? <CheckCircleOutlined style={{ color: "#3f8600", fontSize: 16 }} />
      : <CloseCircleOutlined style={{ color: "#cf1322", fontSize: 16 }} /> },
    { title: "问题", dataIndex: "question", key: "q" },
    { title: "命中位置", dataIndex: "rank", key: "rank", width: 90, render: (r: number | null) => r ? `第 ${r} 位` : "未命中" },
    {
      title: "期望文档", dataIndex: "expected", key: "exp", width: 260,
      render: (e: string[]) => e.map((n, i) => <Tag key={i}>{n}</Tag>),
    },
  ];

  /** 审批规则层详情列（批次2-4）：命中与否 + 期望/实际审批人 */
  const approvalColumns: ColumnsType<EvalApprovalRunResult["approval"]["details"][number]> = [
    {
      title: "结果", key: "hit", width: 70,
      render: (_, r) => r.hit
        ? <CheckCircleOutlined style={{ color: "#3f8600", fontSize: 16 }} />
        : <CloseCircleOutlined style={{ color: "#cf1322", fontSize: 16 }} />,
    },
    { title: "问题", dataIndex: "question", key: "q" },
    {
      title: "期望", dataIndex: "expected", key: "exp", width: 200,
      render: (e: string) => <Tag color="blue">{e}</Tag>,
    },
    {
      title: "规则引擎输出", dataIndex: "got", key: "got", width: 200,
      render: (g: string | null) => g
        ? <Tag color="green">{g}</Tag>
        : <Tag color="default">未命中（回退向量）</Tag>,
    },
  ];

  /** 关系层详情列（批次2-2）：未介入单独标灰，介入题展示实体/路径/邻居 */
  const relationColumns: ColumnsType<EvalRelationRunResult["relation"]["details"][number]> = [
    {
      title: "介入", key: "intervened", width: 70,
      render: (_, r) => r.result
        ? <Tag color="blue">介入</Tag>
        : <Tag color="default">回退向量</Tag>,
    },
    { title: "问题", dataIndex: "question", key: "q" },
    {
      title: "链接实体", key: "entities", width: 200,
      render: (_, r) => r.result?.entities.map((n, i) => <Tag key={i} color="green">{n}</Tag>),
    },
    {
      title: "关系路径", key: "path",
      render: (_, r) => r.result?.path_steps.length
        ? r.result.path_steps.map((s, i) => <div key={i}>{s}</div>)
        : (r.result ? "—（邻居查询）" : ""),
    },
    {
      title: "邻居", key: "neighbors", width: 300,
      render: (_, r) => r.result?.neighbors.map((n, i) => <Tag key={i}>{n}</Tag>),
    },
  ];

  return (
    <Card>
      <Space style={{ marginBottom: 16 }}>
        <Select
          value={evalSet}
          onChange={setEvalSet}
          style={{ width: 160 }}
          options={[
            { value: "all", label: "全部题目" },
            { value: "regression", label: "回归层" },
            { value: "long", label: "语料层" },
            { value: "authority", label: "权威题" },
            { value: "relation", label: "关系题（图谱）" },
            { value: "approval", label: "审批规则题" },
            { value: "raptor", label: "摘要树题（RAPTOR）" },
          ]}
        />
        <Switch checked={withRerank} onChange={setWithRerank} />
        <span>rerank 精排模式</span>
        <Button type="primary" icon={<ExperimentOutlined />} loading={loading} onClick={handleRun}>
          {loading ? "评估中（约 15 秒）..." : "开始评估"}
        </Button>
      </Space>
      {loading && (
        <Alert
          type="info"
          showIcon
          message="正在对全部评估题逐一执行混合检索（每题一次嵌入调用），请稍候..."
          style={{ marginBottom: 16 }}
        />
      )}
      {result && "approval" in result ? (
        <>
          <Alert
            type="info"
            showIcon
            message="审批规则题走规则引擎确定性匹配（金额/天数 → 审批人），与文档命中口径不同"
            style={{ marginBottom: 16 }}
          />
          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col span={4}><Card size="small"><Statistic title="评估题数" value={result.total_questions} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="规则匹配准确率" value={`${(result.approval.accuracy * 100).toFixed(1)}%`} valueStyle={{ color: result.approval.accuracy >= 0.9 ? "#3f8600" : "#faad14" }} /></Card></Col>
          </Row>
          <Table
            columns={approvalColumns}
            dataSource={result.approval.details}
            rowKey={(r) => r.question}
            pagination={false}
            size="small"
          />
        </>
      ) : result && "relation" in result ? (
        <>
          <Alert
            type={result.relation.graph_available ? "success" : "error"}
            showIcon
            message={result.relation.graph_available
              ? "图谱链路可用，已按环节打分（关系题验证的是图谱检索腿，与文档命中口径不同）"
              : "Neo4j 不可用（密码未配置或服务未启动），全部题目未介入——请先启动图谱服务"}
            style={{ marginBottom: 16 }}
          />
          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col span={4}><Card size="small"><Statistic title="评估题数" value={result.total_questions} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="介入率" value={`${(result.relation.intervention_rate * 100).toFixed(1)}%`} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="实体链接准确率" value={`${(result.relation.entity_link_accuracy * 100).toFixed(1)}%`} valueStyle={{ color: result.relation.entity_link_accuracy >= 0.9 ? "#3f8600" : "#faad14" }} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="路径正确率" value={`${(result.relation.path_accuracy * 100).toFixed(1)}%`} valueStyle={{ color: result.relation.path_accuracy >= 0.9 ? "#3f8600" : "#faad14" }} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="邻居召回" value={`${(result.relation.neighbor_accuracy * 100).toFixed(1)}%`} valueStyle={{ color: result.relation.neighbor_accuracy >= 0.9 ? "#3f8600" : "#faad14" }} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="不介入正确率" value={`${(result.relation.no_intervention_accuracy * 100).toFixed(1)}%`} /></Card></Col>
          </Row>
          <Table
            columns={relationColumns}
            dataSource={result.relation.details}
            expandable={{
              // 清单 5.3：关系题逐题可展开——未介入题显示原因，
              // 介入题显示实体/路径/邻居原始数据
              expandedRowRender: (r) => (
                <div style={{ padding: "4px 8px" }}>
                  <Text code style={{ fontSize: 12 }}>
                    {JSON.stringify((r as { result?: unknown }).result ?? "图谱未介入（静默回退纯向量）")}
                  </Text>
                </div>
              ),
            }}
            rowKey={(r) => r.question}
            pagination={false}
            size="small"
          />
        </>
      ) : result && (
        <>
          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col span={4}><Card size="small"><Statistic title="评估题数" value={result.total_questions} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="可见文档" value={result.doc_count} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="Hit-rate@6" value={`${(result.total.hit_rate * 100).toFixed(1)}%`} valueStyle={{ color: result.total.hit_rate >= 0.9 ? "#3f8600" : "#faad14" }} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="MRR@6" value={result.total.mrr.toFixed(3)} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="平均延迟" value={`${result.total.avg_ms.toFixed(0)}ms`} /></Card></Col>
            <Col span={4}><Card size="small"><Statistic title="总耗时" value={`${(result.elapsed_ms / 1000).toFixed(1)}s`} /></Card></Col>
          </Row>
          <Tabs
            items={[
              { key: "standard", label: `标准题（${result.standard.n}）`, children: (
                <Table columns={groupColumns} dataSource={result.standard.details}
                  rowKey={(r) => r.question} pagination={false} size="small"
                  expandable={{
                    // 清单 5.3：逐题可展开——未命中题显示实际检索 top6
                    // （诊断"检索到错文档还是没检索到"）
                    expandedRowRender: (r) => (
                      <div style={{ padding: "4px 8px" }}>
                        <Text strong>实际检索 Top6：</Text>
                        {((r as { got?: string[] }).got ?? []).length === 0
                          ? <Text type="secondary">（无结果）</Text>
                          : ((r as { got: string[] }).got.map((g, i) => (
                            <Tag key={i} color={i === 0 ? "blue" : "default"}>
                              {i + 1}. {g}
                            </Tag>
                          )))}
                      </div>
                    ),
                  }}
                />
              ) },
              { key: "fuzzy", label: `模糊题（${result.fuzzy.n}）`, children: (
                <Table columns={groupColumns} dataSource={result.fuzzy.details}
                  rowKey={(r) => r.question} pagination={false} size="small"
                  expandable={{
                    expandedRowRender: (r) => (
                      <div style={{ padding: "4px 8px" }}>
                        <Text strong>实际检索 Top6：</Text>
                        {((r as { got?: string[] }).got ?? []).length === 0
                          ? <Text type="secondary">（无结果）</Text>
                          : ((r as { got: string[] }).got.map((g, i) => (
                            <Tag key={i} color={i === 0 ? "blue" : "default"}>
                              {i + 1}. {g}
                            </Tag>
                          )))}
                      </div>
                    ),
                  }}
                />
              ) },
            ]}
          />
        </>
      )}
    </Card>
  );
};

const EvalPage: React.FC = () => {
  return (
    <div style={{ padding: 24 }}>
      <PageHeader title="检索评估" description="试检索调参可视化 + 评估集跑分（检索质量的红线尺子）" />
      <Tabs
        items={[
          { key: "test", label: "🔍 试检索", children: <RetrievalTestTab /> },
          { key: "run", label: "📊 评估跑分", children: <EvalRunTab /> },
        ]}
      />
    </div>
  );
};

export default EvalPage;
