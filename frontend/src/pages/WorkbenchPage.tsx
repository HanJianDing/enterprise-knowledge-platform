import React, { useEffect, useState } from "react";
import { Alert, Button, Card, Input, Skeleton, Statistic, Tag } from "antd";
import { ArrowRightOutlined, BookOutlined, FileSearchOutlined, MessageOutlined, SendOutlined, UserOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "../stores/authStore";
import { adminApi, type DashboardData } from "../services/adminApi";
import { documentApi } from "../services/documentApi";
import { chatApi } from "../services/chatApi";
import type { KnowledgeStats } from "../types/document";
import type { Conversation } from "../types/chat";
import { DEFAULT_QUESTIONS, normalizeQuickQuestions } from "../utils/quickQuestions";

const WorkbenchPage: React.FC = () => {
  const navigate = useNavigate();
  const { user, isAdmin, isAuditViewer } = useAuthStore();
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [recent, setRecent] = useState<Conversation[]>([]);
  const [questions, setQuestions] = useState(DEFAULT_QUESTIONS);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(true);
  const [errors, setErrors] = useState<string[]>([]);
  useEffect(() => {
    let active = true;
    setLoading(true); setErrors([]); setRecent([]); setDashboard(null); setStats(null); setQuestions(DEFAULT_QUESTIONS);
    const fail = (text: string) => { if (active) setErrors((old) => [...old, text]); };
    const tasks = [
      (isAdmin ? adminApi.getDashboard().then((v) => { if (active) setDashboard(v); })
        : documentApi.getStats().then((v) => { if (active) setStats(v); })).catch(() => fail("概览加载失败")),
      chatApi.getConversations().then((v) => { if (active) setRecent(v.items.slice(0, 4)); }).catch(() => fail("最近会话加载失败")),
      adminApi.getQuickQuestions().then((v) => { if (active) setQuestions(normalizeQuickQuestions(v.questions)); }).catch(() => {}),
    ];
    Promise.allSettled(tasks).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [isAdmin, user?.id]);
  const ask = (text: string) => navigate("/chat", { state: { draft: text.trim() } });
  return (
    <div className="workbench page-shell">
      <header className="workbench-heading">
        <div><div className="eyebrow">我的工作空间</div><h1>你好，{user?.username || "用户"}<Tag color="blue">{isAdmin ? "管理员" : isAuditViewer ? "审计员" : "员工"}</Tag></h1>
          <p>从一个问题开始，让企业知识更容易找到。</p></div>
        <span className="workbench-date">{new Date().toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "long" })}</span>
      </header>
      <section className="ask-hero">
        <div className="eyebrow">企业知识助手</div><h2>今天，有什么想了解的？</h2>
        <p>查询公司制度、流程和办事指南，结合回答中的来源核对具体要求。</p>
        <div className="hero-input">
          <Input.TextArea aria-label="向知识助手提问" placeholder="例如：出差报销需要准备哪些材料？" value={question}
            onChange={(e) => setQuestion(e.target.value)} autoSize={{ minRows: 2, maxRows: 4 }} maxLength={2000} />
          <Button type="primary" size="large" icon={<SendOutlined />} onClick={() => ask(question)} disabled={!question.trim()}>开始提问</Button>
        </div>
        <div className="hero-hint"><BookOutlined /> 回答范围取决于你的知识库访问权限</div>
      </section>
      {errors.length > 0 && <Alert type="warning" showIcon message={errors.join("；") + "，请刷新页面重试。"} style={{ marginTop: 20 }} />}
      <div className="workbench-grid">
        <Card title="常见问题" className="workbench-panel" extra={<span className="muted">点击后可编辑再发送</span>}>
          <div className="question-grid">{questions.map((q) => <button key={q} className="question-link" onClick={() => ask(q)}><span>{q}</span><ArrowRightOutlined /></button>)}</div>
        </Card>
        <Card title="最近会话" className="workbench-panel" extra={<Button type="link" onClick={() => navigate("/chat")}>全部会话</Button>}>
          {loading ? <Skeleton active paragraph={{ rows: 3 }} /> : recent.length ? recent.map((c) =>
            <button className="recent-conversation" key={c.id} onClick={() => navigate(`/chat/${c.id}`)}>
              <MessageOutlined /><span><strong>{c.title}</strong><small>{new Date(c.updated_at).toLocaleDateString("zh-CN")} · {c.message_count} 条消息</small></span><ArrowRightOutlined />
            </button>) : <div className="quiet-empty">{errors.includes("最近会话加载失败") ? "暂时无法读取会话" : "还没有会话，从上方的问题开始吧"}</div>}
        </Card>
      </div>
      {isAdmin && <section className="management-shortcuts" aria-label="管理快捷入口">
        <Button icon={<BookOutlined />} onClick={() => navigate("/knowledge")}>知识库管理</Button>
        <Button icon={<UserOutlined />} onClick={() => navigate("/admin/users")}>用户管理</Button>
        <Button icon={<ArrowRightOutlined />} onClick={() => navigate("/admin/dashboard")}>系统概览</Button>
      </section>}
      {isAuditViewer && <Button style={{ marginTop: 16 }} icon={<FileSearchOutlined />} onClick={() => navigate("/admin/audit-logs")}>审计日志</Button>}
      <section className="overview-strip" aria-label="数据概览">
        {isAdmin ? <>
          <Statistic title="知识文档" value={dashboard?.document_count ?? "—"} /><Statistic title="已完成处理" value={dashboard?.documents_completed ?? "—"} />
          <Statistic title="处理中" value={dashboard?.documents_processing ?? "—"} /><Statistic title="处理失败" value={dashboard?.documents_failed ?? "—"} />
        </> : <>
          <Statistic title="可见文档" value={stats?.total_documents ?? "—"} /><Statistic title="已完成处理" value={stats?.completed_documents ?? "—"} />
          <div className="scope-note"><BookOutlined /><span>知识按权限开放<br /><small>需要更多资料？请联系知识库管理员。</small></span></div>
        </>}
      </section>
    </div>
  );
};
export default WorkbenchPage;
