import React, { useEffect, useState } from "react";
import { Card, Row, Col, Statistic, Spin, Input, Button, Space, Segmented } from "antd";
import {
  UserOutlined, MessageOutlined, FileTextOutlined,
  CheckCircleOutlined, SyncOutlined, CloseCircleOutlined,
  PlusOutlined, DeleteOutlined, SaveOutlined,
  LikeOutlined, DislikeOutlined, ThunderboltOutlined, BarChartOutlined,
} from "@ant-design/icons";
import { message } from "../../utils/message";
import {
  adminApi,
  type DashboardData,
  type FeedbackStats,
  type DashboardStats,
} from "../../services/adminApi";
import PageHeader from "../../components/common/PageHeader";
import EChart from "../../components/charts/EChart";
import {
  buildTrendOption,
  buildFeedbackPie,
  buildCategoryBar,
} from "./dashboardCharts";

const QUICK_QUESTION_MAX = 8;
const QUICK_QUESTION_MAX_LEN = 30;

const DashboardPage: React.FC = () => {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  // 欢迎页快捷问题（欢迎页显示内容，保存后即时生效）
  const [questions, setQuestions] = useState<string[]>([]);
  const [questionsLoaded, setQuestionsLoaded] = useState(false);
  const [savingQuestions, setSavingQuestions] = useState(false);
  // 满意度统计（赞/踩计数与占比）
  const [feedbackStats, setFeedbackStats] = useState<FeedbackStats | null>(null);
  // 图表看板数据（趋势/质量/分类分布）
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [trendDays, setTrendDays] = useState<7 | 30>(7);
  const [statsLoading, setStatsLoading] = useState(false);

  useEffect(() => {
    adminApi.getDashboard()
      .then(setData)
      .finally(() => setLoading(false));
    adminApi.getQuickQuestions()
      .then((r) => setQuestions(r.questions))
      .catch(() => message.error("加载快捷问题失败"))
      .finally(() => setQuestionsLoaded(true));
    adminApi.getFeedbackStats()
      .then(setFeedbackStats)
      .catch(() => {});
  }, []);

  // 趋势切换 7/30 天时重新拉取
  useEffect(() => {
    setStatsLoading(true);
    adminApi.getDashboardStats(trendDays)
      .then(setStats)
      .catch(() => message.error("加载看板数据失败"))
      .finally(() => setStatsLoading(false));
  }, [trendDays]);

  const handleSaveQuestions = async () => {
    const cleaned = questions.map((q) => q.trim()).filter((q) => q !== "");
    if (cleaned.length === 0) {
      message.error("至少保留一条快捷问题");
      return;
    }
    if (cleaned.length > QUICK_QUESTION_MAX) {
      message.error(`快捷问题最多 ${QUICK_QUESTION_MAX} 条`);
      return;
    }
    if (cleaned.some((q) => q.length > QUICK_QUESTION_MAX_LEN)) {
      message.error(`每条快捷问题不超过 ${QUICK_QUESTION_MAX_LEN} 字`);
      return;
    }
    setSavingQuestions(true);
    try {
      await adminApi.setQuickQuestions(cleaned);
      setQuestions(cleaned);
      message.success("快捷问题已更新，欢迎页即时生效");
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || "保存失败");
    } finally {
      setSavingQuestions(false);
    }
  };

  if (loading) return <Spin size="large" style={{ display: "block", margin: "100px auto" }} />;

  const rate = feedbackStats?.satisfaction_rate;

  return (
    <div style={{ padding: 24 }}>
      <PageHeader title="系统仪表盘" description="全局运行数据概览" />
      <Row gutter={[16, 16]}>
        <Col span={6}>
          <Card><Statistic title="用户总数" value={data?.user_count || 0} prefix={<UserOutlined />} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title="对话会话" value={data?.conversation_count || 0} prefix={<MessageOutlined />} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title="消息总数" value={data?.message_count || 0} prefix={<MessageOutlined />} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title="文档总数" value={data?.document_count || 0} prefix={<FileTextOutlined />} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title="文档已完成" value={data?.documents_completed || 0} valueStyle={{ color: "#3f8600" }} prefix={<CheckCircleOutlined />} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title="文档处理中" value={data?.documents_processing || 0} valueStyle={{ color: "#1890ff" }} prefix={<SyncOutlined spin />} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title="文档失败" value={data?.documents_failed || 0} valueStyle={{ color: "#cf1322" }} prefix={<CloseCircleOutlined />} /></Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="回答满意度"
              value={rate ?? "--"}
              suffix={rate !== null && rate !== undefined ? "%" : undefined}
              valueStyle={{
                color:
                  (rate ?? 0) >= 80
                    ? "#3f8600"
                    : (rate ?? 0) >= 60
                      ? "#f97316"
                      : "#cf1322",
              }}
            />
            <div style={{ fontSize: 12, color: "#999", marginTop: 6 }}>
              <LikeOutlined style={{ color: "#52c41a", marginRight: 4 }} />
              {feedbackStats?.like ?? 0}
              <DislikeOutlined style={{ color: "#ff4d4f", marginLeft: 12, marginRight: 4 }} />
              {feedbackStats?.dislike ?? 0}
              <span style={{ marginLeft: 8 }}>
                {feedbackStats?.total ? `共 ${feedbackStats.total} 条评价` : "暂无评价"}
              </span>
            </div>
          </Card>
        </Col>
      </Row>

      {/* ── 图表区 ── */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col span={16}>
          <Card
            title="对话趋势"
            extra={
              <Segmented
                options={[
                  { label: "近 7 天", value: 7 },
                  { label: "近 30 天", value: 30 },
                ]}
                value={trendDays}
                onChange={(v) => setTrendDays(v as 7 | 30)}
              />
            }
            loading={statsLoading}
          >
            {stats && stats.trend.length > 0 ? (
              <EChart option={buildTrendOption(stats.trend)} height={280} />
            ) : (
              <div style={{ height: 280, display: "flex", alignItems: "center", justifyContent: "center", color: "#999" }}>
                暂无数据
              </div>
            )}
          </Card>
        </Col>
        <Col span={8}>
          <Card title="消息质量" loading={statsLoading}>
            <Row gutter={16}>
              <Col span={12}>
                <Statistic
                  title="平均回答延迟"
                  value={stats?.quality.avg_latency_ms ?? "--"}
                  suffix={stats?.quality.avg_latency_ms !== null && stats?.quality.avg_latency_ms !== undefined ? "ms" : undefined}
                  prefix={<ThunderboltOutlined />}
                />
              </Col>
              <Col span={12}>
                <Statistic
                  title="平均 Token"
                  value={stats?.quality.avg_tokens ?? "--"}
                  prefix={<BarChartOutlined />}
                />
              </Col>
            </Row>
            <div style={{ marginTop: 16 }}>
              {stats && stats.feedback.total > 0 ? (
                <EChart
                  option={buildFeedbackPie(stats.feedback)}
                  height={200}
                />
              ) : (
                <div style={{ height: 200, display: "flex", alignItems: "center", justifyContent: "center", color: "#999" }}>
                  暂无评价数据
                </div>
              )}
            </div>
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col span={24}>
          <Card title="知识库分类分布" loading={statsLoading}>
            {stats && stats.categories.length > 0 ? (
              <EChart option={buildCategoryBar(stats.categories)} height={220} />
            ) : (
              <div style={{ height: 220, display: "flex", alignItems: "center", justifyContent: "center", color: "#999" }}>
                暂无文档数据
              </div>
            )}
          </Card>
        </Col>
      </Row>

      {/* 欢迎页快捷问题配置 */}
      <Card
        title="欢迎页快捷问题"
        style={{ marginTop: 16 }}
        loading={!questionsLoaded}
        extra={
          <Button
            type="primary"
            icon={<SaveOutlined />}
            loading={savingQuestions}
            onClick={handleSaveQuestions}
          >
            保存
          </Button>
        }
      >
        <div style={{ color: "#999", fontSize: 13, marginBottom: 12 }}>
          用户在聊天页欢迎区看到的推荐问题，点击即问。保存后即时生效。
        </div>
        <Space direction="vertical" style={{ width: "100%" }} size={8}>
          {questions.map((q, i) => (
            <Space.Compact key={i} style={{ width: "100%" }}>
              <Input
                value={q}
                maxLength={QUICK_QUESTION_MAX_LEN}
                placeholder={`问题 ${i + 1}`}
                onChange={(e) =>
                  setQuestions((prev) =>
                    prev.map((item, idx) => (idx === i ? e.target.value : item))
                  )
                }
              />
              <Button
                icon={<DeleteOutlined />}
                onClick={() => setQuestions((prev) => prev.filter((_, idx) => idx !== i))}
                disabled={questions.length <= 1}
                title="删除此条"
              />
            </Space.Compact>
          ))}
          <Button
            type="dashed"
            block
            icon={<PlusOutlined />}
            onClick={() =>
              setQuestions((prev) =>
                prev.length >= QUICK_QUESTION_MAX ? prev : [...prev, ""]
              )
            }
            disabled={questions.length >= QUICK_QUESTION_MAX}
          >
            添加问题（{questions.length}/{QUICK_QUESTION_MAX}）
          </Button>
        </Space>
      </Card>
    </div>
  );
};

export default DashboardPage;
