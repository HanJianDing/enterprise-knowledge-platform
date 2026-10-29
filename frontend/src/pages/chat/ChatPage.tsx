import React, { useEffect, useRef, useState } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import { Layout, Spin, Button, Avatar, Tag, Drawer } from "antd";
import { message } from "../../utils/message";
import {
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  UserOutlined,
  RobotOutlined,
  CustomerServiceOutlined,
} from "@ant-design/icons";
import ChatInput from "../../components/chat/ChatInput";
import StreamingMarkdown from "../../components/chat/StreamingMarkdown";
import CitationPopover from "../../components/chat/CitationPopover";
import SourceDetailPanel from "../../components/chat/SourceDetailPanel";
import SourceDetailDrawer from "../../components/chat/SourceDetailDrawer";
import MessageActions from "../../components/chat/MessageActions";
import { useChatStore } from "../../stores/chatStore";
import { useAuthStore } from "../../stores/authStore";
import { useStreamingChat } from "../../hooks/useStreamingChat";
import { useIsMobile } from "../../hooks/useIsMobile";
import { chatApi } from "../../services/chatApi";
import ConversationSidebar from "./ChatSidebar";
import type { SSESource, Citation } from "../../types/chat";
import { useThemeTokens } from "../../theme/themeStore";
import { DEFAULT_QUESTIONS, normalizeQuickQuestions } from "../../utils/quickQuestions";

const { Sider, Content } = Layout;

/** 空对话欢迎引导（接口失败时的静态兜底，与后端默认种子一致；企业知识库口径） */
const WELCOME_SUGGESTIONS = DEFAULT_QUESTIONS;

/** SSESource 列表转为 Citation 列表（引用展示用，保留 chunk_idx 供"查看上下文"定位） */
function toCitations(sources: SSESource[]): Citation[] {
  return sources.map((s) => ({
    index: s.index,
    doc_id: s.doc_id,
    title: s.title,
    text: s.text,
    category_path: s.category_path,
    chunk_idx: s.chunk_idx,
  }));
}

const ChatPage: React.FC = () => {
  const { id: urlConvId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [draft, setDraft] = useState({ text: (location.state?.draft as string) || "", key: location.key as string | number });
  useEffect(() => { setDraft({ text: (location.state?.draft as string) || "", key: location.key }); }, [location.key]);
  const [collapsed, setCollapsed] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingConversations, setLoadingConversations] = useState(true);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const followOutput = useRef(true);
  const isMobile = useIsMobile();
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);
  const t = useThemeTokens();
  // 引用详情抽屉：点击"来源N"标签后打开，展示该切片原文 + 上下相邻切片
  const [detailCitation, setDetailCitation] = useState<Citation | null>(null);

  // 欢迎页快捷问题（后台可配置；接口失败回退静态默认值）
  const [suggestions, setSuggestions] = useState<string[]>(WELCOME_SUGGESTIONS);

  const {
    conversations,
    currentConversationId,
    messages,
    isStreaming,
    streamContent,
    streamSources,
    classifiedCategory,
    clarifiedQuestion,
    thinkingText,
    setConversations,
    setCurrentConversation,
    setMessages,
    clearStream,
    setStreamSources,
  } = useChatStore();

  const isAdmin = useAuthStore((s) => s.isAdmin);

  const { sendMessage, regenerateLast, stopStreaming } = useStreamingChat();

  useEffect(() => {
    chatApi
      .getConversations()
      .then((r) => setConversations(r.items))
      .catch(() => message.error("加载会话列表失败"))
      .finally(() => setLoadingConversations(false));
    chatApi
      .getQuickQuestions()
      .then((items) => setSuggestions(normalizeQuickQuestions(items)))
      .catch(() => {
        // 接口失败保留静态默认值
      });
  }, []);

  useEffect(() => {
    if (urlConvId && urlConvId !== currentConversationId) {
      handleSelectConversation(urlConvId);
    } else if (!urlConvId && currentConversationId) {
      stopStreaming();
      clearStream();
      setStreamSources([]);
      setCurrentConversation(null);
      setMessages([]);
    }
  }, [urlConvId]);

  useEffect(() => {
    if (followOutput.current) messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamContent]);

  const handleSelectConversation = async (id: string) => {
    followOutput.current = true;
    if (id === currentConversationId) return;
    // 修复：先中断在途 SSE 流，防止旧会话的 token/回调继续写入新会话视图
    stopStreaming();
    navigate(`/chat/${id}`);
    clearStream();
    setStreamSources([]);
    setCurrentConversation(id);
    setMessages([]);
    setLoadingHistory(true);
    try {
      const detail = await chatApi.getConversation(id);
      setMessages(detail.messages || []);
    } catch {
      message.error("加载对话历史失败");
    } finally {
      setLoadingHistory(false);
    }
  };

  const handleNewChat = () => {
    followOutput.current = true;
    // 修复：新建会话同样先中断在途 SSE 流
    stopStreaming();
    navigate("/chat");
    clearStream();
    setStreamSources([]);
    setCurrentConversation(null);
    setMessages([]);
  };

  const handleDeleteConversation = async (id: string) => {
    try {
      await chatApi.deleteConversation(id);
      setConversations(conversations.filter((c) => c.id !== id));
      if (id === currentConversationId) {
        setCurrentConversation(null);
        setMessages([]);
        navigate("/chat", { replace: true });
      }
      message.success("会话已删除");
    } catch (e) {
      // 后端语义（如"人工服务进行中，暂不能删除"）透传给用户
      const detail = (e as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      message.error(detail || "删除失败");
    }
  };

  const handleRenameConversation = async (id: string, title: string) => {
    const trimmed = title.trim();
    if (!trimmed) return;
    try {
      await chatApi.updateConversation(id, { title: trimmed });
      setConversations(
        conversations.map((c) => (c.id === id ? { ...c, title: trimmed } : c))
      );
    } catch {
      message.error("重命名失败");
    }
  };

  const handleClearAll = async () => {
    try {
      const resp = await chatApi.clearAllConversations();
      // 重新拉取列表
      const list = await chatApi.getConversations();
      setConversations(list.items);
      setCurrentConversation(null);
      setMessages([]);
      navigate("/chat");
      message.success(resp.message);
    } catch {
      message.error("清空失败");
    }
  };

  const handleSend = (text: string) => {
    followOutput.current = true;
    sendMessage(text);
  };

  const isEmpty = messages.length === 0 && !isStreaming && !loadingHistory;

  // 最后一条 assistant 消息 id（决定"重新生成"按钮显隐）
  const lastAssistantId = [...messages]
    .reverse()
    .find((m) => m.role === "assistant")?.id;

  const sidebarContent = (
    <ConversationSidebar
      conversations={conversations}
      currentId={currentConversationId}
      collapsed={false}
      loading={loadingConversations}
      onSelect={(id) => {
        handleSelectConversation(id);
        setMobileDrawerOpen(false);
      }}
      onDelete={handleDeleteConversation}
      onNewChat={() => {
        handleNewChat();
        setMobileDrawerOpen(false);
      }}
      onRename={handleRenameConversation}
      onClearAll={handleClearAll}
    />
  );

  return (
    <Layout className="chat-layout" style={{ overflow: "hidden" }}>
      {/* 左侧会话列表：桌面 Sider / 移动端 Drawer */}
      {isMobile ? (
        <Drawer
          placement="left"
          width={280}
          open={mobileDrawerOpen}
          onClose={() => setMobileDrawerOpen(false)}
          title="对话列表"
          styles={{ body: { padding: 0, height: "100%" } }}
        >
          {sidebarContent}
        </Drawer>
      ) : (
        <Sider
          collapsible
          collapsed={collapsed}
          onCollapse={setCollapsed}
          width={240}
          collapsedWidth={0}
          style={{ background: t.color.sidebarBg, borderRight: `1px solid ${t.color.borderBase}` }}
          trigger={null}
        >
          {sidebarContent}
        </Sider>
      )}

      {/* 主对话区 */}
      <Layout style={{ minWidth: 0 }}>
        <Content
          style={{
            display: "flex",
            flexDirection: "column",
            height: "100%",
            minWidth: 0,
            background: t.color.contentBg,
          }}
        >
          {/* 顶部栏 — 极简化 */}
          <div
            style={{
              padding: "10px 20px",
              background: t.color.white,
              borderBottom: `1px solid ${t.color.borderLight}`,
              display: "flex",
              alignItems: "center",
              gap: 12,
              flexShrink: 0,
            }}
          >
            <Button
              type="text"
              aria-label={isMobile || collapsed ? "展开历史会话" : "收起历史会话"}
              icon={
                isMobile || collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />
              }
              onClick={() =>
                isMobile
                  ? setMobileDrawerOpen(true)
                  : setCollapsed(!collapsed)
              }
            />
            <span
              style={{
                flex: 1,
                textAlign: "center",
                color: t.color.textPrimary,
                fontSize: 14,
                fontWeight: 500,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {currentConversationId
                ? conversations.find((c) => c.id === currentConversationId)
                    ?.title || "对话"
                : "新对话"}
            </span>
            {/* 占位保持居中 */}
            <Button type="text" onClick={handleNewChat}>新对话</Button>
          </div>

          {/* 消息区域 */}
          <div
            className="messages-scroll"
            ref={scrollRef}
            onScroll={() => { const el = scrollRef.current; if (el) followOutput.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100; }}
            style={{
              flex: 1,
              overflow: "auto",
              padding: isEmpty ? "24px 32px" : "20px 32px 0",
            }}
          >
            {loadingHistory ? (
              <div style={{ textAlign: "center", padding: 60 }}>
                <Spin tip="加载对话中..." />
              </div>
            ) : isEmpty ? (
              /* ── 空对话欢迎区 ── */
              <div style={{ maxWidth: 640, margin: "0 auto", textAlign: "center", paddingTop: "8vh" }}>
                <div className="welcome-brand-icon"><RobotOutlined /></div>
                <h2 style={{ marginBottom: 8, color: t.color.textPrimary }}>
                  让知识，成为工作的帮手
                </h2>
                <p style={{ color: t.color.textSecondary, fontSize: 14, marginBottom: 28, lineHeight: 1.8 }}>
                  查询制度、了解流程，从你有权访问的知识中寻找答案。
                  <br />
                  选择一个问题，编辑后发送。
                </p>
                <div className="welcome-grid">
                  {suggestions.map((q) => (
                    <button
                      key={q}
                      className="welcome-card"
                      onClick={() => setDraft({ text: q, key: Date.now() })}
                    >
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              /* ── 消息列表 ── */
              <div className="chat-column">
                {messages.map((msg) => {
                  // system 消息（历史提示类）居中灰色提示条
                  if (msg.role === "system") {
                    return (
                      <div key={msg.id} style={{ textAlign: "center", marginBottom: 16 }}>
                        <span
                          style={{
                            display: "inline-block",
                            background: "#f0f0f0",
                            color: "#888",
                            padding: "4px 16px",
                            borderRadius: 12,
                            fontSize: 12,
                            maxWidth: "70%",
                          }}
                        >
                          {msg.content}
                        </span>
                      </div>
                    );
                  }

                  const isUser = msg.role === "user";
                  // agent=客服时代的历史人工回复（阶段2后不再产生，仅历史数据展示）
                  const isAgent = msg.role === "agent";
                  return (
                    <div
                      key={msg.id}
                      style={{
                        marginBottom: 20,
                        display: "flex",
                        justifyContent: isUser ? "flex-end" : "flex-start",
                        alignItems: "flex-start",
                        gap: 10,
                      }}
                    >
                      {/* AI / 人工客服 头像 */}
                      {!isUser && (
                        <Avatar
                          icon={isAgent ? <CustomerServiceOutlined /> : <RobotOutlined />}
                          style={{
                            background: isAgent ? t.color.brandBg : t.color.primaryBg,
                            color: isAgent ? t.color.brand : t.color.primary,
                            flexShrink: 0,
                            marginTop: 4,
                          }}
                        />
                      )}

                      <div
                        style={{
                          maxWidth: isUser ? "85%" : "calc(100% - 42px)",
                          minWidth: 0,
                          padding: "12px 18px",
                          borderRadius: isUser ? "16px 4px 16px 16px" : "4px 16px 16px 16px",
                          background:
                            isUser
                              ? `linear-gradient(135deg, ${t.color.primary}, ${t.color.primaryHover})`
                              : t.color.white,
                          color: isUser ? "#fff" : t.color.textPrimary,
                          lineHeight: 1.8,
                          boxShadow:
                            isUser
                              ? t.shadow.userBubble
                              : t.shadow.bubble,
                          border: !isUser ? `1px solid ${t.color.borderLight}` : "none",
                        }}
                      >
                        {isAgent && (
                          <div style={{ fontSize: 11, color: t.color.brand, marginBottom: 4 }}>
                            👩‍💼 人工客服
                          </div>
                        )}
                        {isUser ? (
                          msg.content
                        ) : (
                          <>
                            <StreamingMarkdown content={msg.content} />
                            {/* 企业知识库：引用出处对所有角色可见（答案要有出处可查证）；
                                来源详情面板仅管理员（验收 A4：员工只需要答案与引用标签） */}
                            <CitationPopover citations={msg.citations || []} onDetail={setDetailCitation} />
                            {isAdmin && <SourceDetailPanel citations={msg.citations || []} />}
                            {isAdmin && msg.latency_ms && (
                              <details
                                style={{
                                  fontSize: 11,
                                  color: "#bbb",
                                  marginTop: 8,
                                }}
                              >
                                <summary style={{ cursor: "pointer" }}>回答诊断</summary>
                                {msg.stage_times
                                  ? `⏱ ${msg.latency_ms}ms · ${msg.token_count || 0} tokens · 阶段 分类${msg.stage_times.classify_ms}/检索${msg.stage_times.retrieve_ms}/生成${msg.stage_times.generate_ms}ms`
                                  : `⏱ ${msg.latency_ms}ms · ${msg.token_count || 0} chars`}
                                {msg.trace_id && (
                                  <a
                                    href={`${import.meta.env.VITE_JAEGER_URL || "http://localhost:16686"}/trace/${msg.trace_id}`}
                                    target="_blank"
                                    rel="noreferrer"
                                    style={{ marginLeft: 8, fontSize: 11 }}
                                    title="在 Jaeger 中查看本次问答链路"
                                  >
                                    🔍 trace
                                  </a>
                                )}
                              </details>
                            )}
                            {/* 消息操作：复制 / 重新生成（仅 AI 回答；流式临时消息无真实 id 不渲染） */}
                            {msg.role === "assistant" && !msg.id.startsWith("temp-") && (
                              <MessageActions
                                message={msg}
                                canRegenerate={
                                  msg.id === lastAssistantId &&
                                  !isStreaming
                                }
                                onRegenerate={regenerateLast}
                              />
                            )}
                          </>
                        )}
                      </div>

                      {/* 用户头像 */}
                      {isUser && (
                        <Avatar
                          icon={<UserOutlined />}
                          style={{
                            background: t.color.primary,
                            color: "#fff",
                            flexShrink: 0,
                            marginTop: 4,
                          }}
                        />
                      )}
                    </div>
                  );
                })}

                {/* 流式渲染中 */}
                {isStreaming && (
                  <div
                    style={{
                      marginBottom: 20,
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "flex-start",
                      gap: 8,
                    }}
                  >
                    {/* 进度提示（后端 thinking 事件） */}
                    {thinkingText && (
                      <div className="thinking-row">
                        <Spin size="small" /> {thinkingText}
                      </div>
                    )}
                    {/* 分类识别标签（管理员可见） */}
                    {isAdmin && classifiedCategory && (
                      <div style={{ marginLeft: 42, marginBottom: 2 }}>
                        <Tag
                          color="orange"
                          style={{ fontSize: 12, borderRadius: 12, padding: "2px 10px" }}
                        >
                          🏷️ 分类：{classifiedCategory}
                        </Tag>
                        {clarifiedQuestion && clarifiedQuestion !== "" && (
                          <span style={{ fontSize: 11, color: "#999", marginLeft: 4 }}>
                            💡 检索：{clarifiedQuestion}
                          </span>
                        )}
                      </div>
                    )}
                    <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
                      <Avatar
                        icon={<RobotOutlined />}
                        style={{
                          background: t.color.primaryBg,
                          color: t.color.primary,
                          flexShrink: 0,
                          marginTop: 4,
                        }}
                      />
                      <div
                        style={{
                          maxWidth: "calc(100% - 42px)",
                          minWidth: 0,
                          padding: "12px 18px",
                          borderRadius: "4px 16px 16px 16px",
                          background: t.color.white,
                          lineHeight: 1.8,
                          boxShadow: t.shadow.bubble,
                          border: `1px solid ${t.color.borderLight}`,
                        }}
                      >
                      <StreamingMarkdown
                        content={streamContent}
                        isStreaming={true}
                      />
                      {isAdmin && streamSources && streamSources.length > 0 && (
                        <>
                          <CitationPopover citations={toCitations(streamSources)} onDetail={setDetailCitation} />
                          <SourceDetailPanel citations={toCitations(streamSources)} />
                        </>
                      )}
                    </div>
                    </div>
                  </div>
                )}

                <div ref={messagesEndRef} />
              </div>
            )}
          </div>

          {/* 底部输入区 — 固定 + 阴影 */}
          <div
            className="chat-input-shell"
            style={{
              flexShrink: 0,
              padding: "12px 32px 20px",
              background: t.color.white,
              borderTop: `1px solid ${t.color.borderLight}`,
              boxShadow: t.shadow.inputUp,
            }}
          >
            <div className="chat-column">
              <ChatInput
                draft={draft}
                onSend={handleSend}
                disabled={false}
                isStreaming={isStreaming}
                onStop={stopStreaming}
                placeholder="输入您的问题，Enter 发送，Shift+Enter 换行..."
              />
              <div className="composer-hint">Enter 发送 · Shift + Enter 换行 · 重要信息请结合来源核对</div>
            </div>
          </div>
        </Content>
      </Layout>

      {/* 引用详情抽屉：点击"来源N"标签打开，展示切片原文 + 上下相邻切片（答案可查证） */}
      <SourceDetailDrawer
        citation={detailCitation}
        onClose={() => setDetailCitation(null)}
      />
    </Layout>
  );
};

export default ChatPage;
