import { useCallback, useEffect, useRef } from "react";
import { message } from "../utils/message";
import { useChatStore } from "../stores/chatStore";
import { useAuthStore } from "../stores/authStore";
import { chatApi } from "../services/chatApi";
import type { SSEEvent, ChatMessage, SSESource, Citation } from "../types/chat";

export function useStreamingChat() {
  const {
    isStreaming,
    streamContent,
    streamSources,
    messages,
    currentConversationId,
    setIsStreaming,
    appendStreamContent,
    setStreamSources,
    setClassifiedCategory,
    setClarifiedQuestion,
    setThinkingText,
    clearStream,
    addMessage,
    setCurrentConversation,
  } = useChatStore();

  const abortRef = useRef<AbortController | null>(null);
  // 乐观添加的临时用户消息 id：失败/401 时回滚（后端并未保存该消息）
  const tempIdRef = useRef<string | null>(null);

  /** SSE 事件处理（streamChat 与 regenerate 共用同一套状态机） */
  const handleSSEEvent = useCallback(
    (event: SSEEvent) => {
      switch (event.type) {
        case "status":
          // 后端 thinking 事件：进度提示（"正在理解问题并检索知识库..."）
          setThinkingText(event.message || "");
          break;
        case "classified":
          setClassifiedCategory(event.category_name || "");
          setClarifiedQuestion(event.clarified_question || "");
          break;
        case "sources":
          setStreamSources(event.documents || []);
          break;
        case "text_delta":
          appendStreamContent(event.content || "");
          break;
        case "done":
          // 权威内容已由 getConversation 重拉，乐观的临时消息 id 已完成使命
          tempIdRef.current = null;
          // 先保存流式内容，clearStream 之后会清空
          // user 角色：done 携带后处理（去引用/截断）后的权威内容，优先使用
          const finalContent =
            event.content !== undefined
              ? event.content
              : useChatStore.getState().streamContent;
          const finalSources = useChatStore.getState().streamSources || [];
          const finalConvId = event.conversation_id || "";
          if (!currentConversationId && finalConvId) {
            setCurrentConversation(finalConvId);
          }
          clearStream();
          setIsStreaming(false);

          // 从后端获取正确保存的消息（含过滤后的 citations）
          if (finalConvId) {
            chatApi
              .getConversation(finalConvId)
              .then((detail) => {
                useChatStore.getState().setMessages(detail.messages || []);
                // 仅对新会话：追加到列表头部，不用全量替换（避免覆盖刚发生的删除）
                if (!currentConversationId) {
                  const convs = useChatStore.getState().conversations;
                  if (!convs.find((c) => c.id === finalConvId)) {
                    useChatStore.getState().setConversations([
                      {
                        id: finalConvId,
                        title: detail.title || "",
                        status: detail.status || "active",
                        message_count: detail.message_count || 2,
                        created_at: detail.created_at || new Date().toISOString(),
                        updated_at: detail.updated_at || new Date().toISOString(),
                      },
                      ...convs,
                    ]);
                  }
                }
              })
              .catch(() => {
                // 回退：用保存的流式内容自己构造
                const aiMsg: ChatMessage = {
                  id: event.message_id || `msg-${Date.now()}`,
                  conversation_id: finalConvId,
                  role: "assistant",
                  content: finalContent,
                  citations: _extractCitations(finalContent, finalSources),
                  latency_ms: event.latency_ms,
                  token_count: event.token_count,
                  stage_times: event.stage_times,
                  trace_id: event.trace_id,
                  token_usage: event.token_usage,
                  created_at: new Date().toISOString(),
                };
                useChatStore.getState().addMessage(aiMsg);
              });
          }
          break;
        case "error":
          setIsStreaming(false);
          clearStream();
          // 批次F 验收修复：后端 error 事件此前被静默吞掉——嵌入 API
          // 异常等故障时流式气泡直接消失、用户无任何感知（真实验收
          // 8.3）；后端已带可读消息，此处必须展示给用户
          message.error(event.message || "生成回答时出错，请稍后重试");
          break;
      }
    },
    [
      currentConversationId,
      setClassifiedCategory,
      setClarifiedQuestion,
      setThinkingText,
      setStreamSources,
      appendStreamContent,
      clearStream,
      setIsStreaming,
      addMessage,
      setCurrentConversation,
    ]
  );

  const sendMessage = useCallback(
    (text: string) => {
      if (isStreaming || !text.trim()) return;
      clearStream();
      setIsStreaming(true);

      // 添加用户消息
      const userMsg: ChatMessage = {
        id: `temp-${Date.now()}`,
        conversation_id: currentConversationId || "",
        role: "user",
        content: text,
        created_at: new Date().toISOString(),
      };
      tempIdRef.current = userMsg.id;
      addMessage(userMsg);

      const role = useAuthStore.getState().isAdmin ? "admin" : "user";

      const controller = chatApi.streamChat(
        text,
        currentConversationId,
        role,
        handleSSEEvent,
        (error) => {
          setIsStreaming(false);
          clearStream();
          // 失败回滚：移除乐观添加的临时用户消息（后端并未保存）
          if (tempIdRef.current) {
            useChatStore.getState().setMessages(
              useChatStore
                .getState()
                .messages.filter((m) => m.id !== tempIdRef.current)
            );
            tempIdRef.current = null;
          }
          message.error(`对话失败: ${error}`);
        },
        () => {
          setIsStreaming(false);
        }
      );

      abortRef.current = controller;
    },
    [
      currentConversationId,
      isStreaming,
      handleSSEEvent,
      clearStream,
      setIsStreaming,
      addMessage,
    ]
  );

  /** 重新生成最后一条回答：乐观移除旧回答 → 流式重答（done 后 getConversation 对账） */
  const regenerateLast = useCallback(() => {
    if (isStreaming || !currentConversationId) return;
    const msgs = useChatStore.getState().messages;
    const last = msgs[msgs.length - 1];
    if (!last || last.role !== "assistant") return;

    // 乐观移除最后一条 assistant 消息（后端删除旧回答后重新生成）
    useChatStore.getState().setMessages(msgs.slice(0, -1));
    clearStream();
    setIsStreaming(true);

    const role = useAuthStore.getState().isAdmin ? "admin" : "user";
    const controller = chatApi.regenerate(
      currentConversationId,
      role,
      handleSSEEvent,
      (error) => {
        setIsStreaming(false);
        clearStream();
        message.error(`重新生成失败: ${error}`);
        // 失败时把乐观移除的消息捞回来（重新拉取权威列表）
        chatApi
          .getConversation(currentConversationId)
          .then((detail) => useChatStore.getState().setMessages(detail.messages || []))
          .catch(() => {});
      },
      () => {
        setIsStreaming(false);
      }
    );

    abortRef.current = controller;
  }, [currentConversationId, isStreaming, handleSSEEvent, clearStream, setIsStreaming]);

  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
    // 修复：abort 后后端不会保存 assistant 消息，残留的半截流式内容与库不一致，必须丢弃
    clearStream();
    setIsStreaming(false);
  }, [clearStream, setIsStreaming]);

  // 质检 P3-4：组件卸载时中止进行中的流——否则 150s 空闲超时计时器
  // 残留，卸载后仍可能弹出"回答生成超时"toast。质检 P3-4 复核 P2：
  // 卸载落在 fetch 尚未返回阶段时 AbortError 被静默吞掉、onDone 不触发，
  // isStreaming 卡 true 会拦住重挂载后的 sendMessage——此处与
  // stopStreaming 对齐复位
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      clearStream();
      setIsStreaming(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 批次F 验收：流式生成期间刷新/关闭页面弹浏览器确认——生成中途
  // 中断后端不会保存回答（半截答案不入库是设计），用户不知情刷新
  // 会"回答消失"（真实验收）；确认提示让用户知情决策
  useEffect(() => {
    if (!isStreaming) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isStreaming]);

  return {
    isStreaming,
    streamContent,
    streamSources,
    messages,
    sendMessage,
    regenerateLast,
    stopStreaming,
  };
}

/** 从回答文本中提取引用标记 [来源N]，匹配来源文档（与后端逻辑一致） */
function _extractCitations(
  answerText: string,
  sources: SSESource[]
): Citation[] {
  const citations: Citation[] = [];
  const matches = answerText.matchAll(/\[来源(\d+)\]/g);
  const seen = new Set<number>();

  for (const m of matches) {
    if (m[1] === undefined) continue; // 严格模式下 matchAll 捕获组可能为 undefined
    const idx = parseInt(m[1], 10);
    if (seen.has(idx) || idx < 1 || idx > sources.length) continue;
    seen.add(idx);
    const src = sources[idx - 1];
    if (!src) continue; // 严格模式下按索引取数组元素可能为 undefined
    citations.push({
      index: idx,
      doc_id: src.doc_id,
      title: src.title,
      text: src.text,
      category_path: src.category_path,
    });
  }
  return citations;
}
