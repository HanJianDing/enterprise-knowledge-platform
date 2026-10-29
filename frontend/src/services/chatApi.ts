import api, { refreshAccessToken } from "./api";
import type {
  Conversation,
  ConversationDetail,
  PaginatedResponse,
  SSEEvent,
} from "../types/chat";

/** SSE 流式请求公共实现：手动解析 event:/data: 帧（streamChat / regenerate 共用）
 *
 * 上线安全修复：401 时走单飞刷新（与 axios 拦截器共用），刷新成功自动重试一次；
 * 刷新失败（过期/顶号）提示"登录已过期"（store 已同步登出）。
 */
function streamSSE(
  path: string,
  body: Record<string, unknown>,
  onEvent: (event: SSEEvent) => void,
  onError: (error: string) => void,
  onDone: () => void
): AbortController {
  const controller = new AbortController();

  const doRequest = async (): Promise<boolean> => {
    const token = localStorage.getItem("access_token");
    const response = await fetch(`/api/v1${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });

    // 401：交给调用方走刷新重试，其余错误直接报
    if (response.status === 401) return false;
    if (!response.ok) {
      onError(`请求失败 (${response.status})`);
      return true;
    }

    const reader = response.body?.getReader();
    if (!reader) {
      onError("无法读取响应流");
      return true;
    }

    const decoder = new TextDecoder();
    let buffer = "";

    // 批次F 验收修复：后端 LLM 调用挂起时（生成阶段有 120s 超时兜底）
    // SSE 会长时间无任何事件——此前前端 read() 无限等待，用户只能看到
    // "检索后静默"（真实验收事故：dashscope 网络波动时生成挂起，
    // 前端无限静默）。空闲 150s（> 后端 120s 兜底）判定挂死：中止并提示。
    let idleTimer: ReturnType<typeof setTimeout> | null = null;
    // 质检 P3-4：settled 标志——超时/错误路径已回调后不再触发 onDone
    // （此前超时 onError 与循环结束 onDone 双触发，契约漂移）
    let settled = false;
    const resetIdle = () => {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        settled = true;
        controller.abort();
        onError("回答生成超时，请重试");
      }, 150_000);
    };
    resetIdle();

    while (true) {
      let readResult: { done: boolean; value?: Uint8Array };
      try {
        readResult = await reader.read();
      } catch {
        // 中止（空闲超时/用户刷新/组件卸载）静默结束；非中止的网络
        // 错误给明确提示（区别于"静默"）
        if (!controller.signal.aborted) {
          settled = true;
          onError("连接中断，请重试");
        }
        break;
      }
      const { done, value } = readResult;
      if (done) break;
      resetIdle();

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      let currentEvent = "";
      for (const line of lines) {
        if (line.startsWith("event: ")) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith("data: ")) {
          try {
            const data = JSON.parse(line.slice(6));
            onEvent({ ...data, _event: currentEvent });
          } catch {
            // 忽略解析错误
          }
        }
      }
    }
    if (idleTimer) clearTimeout(idleTimer);
    if (!settled) {
      onDone();
    }
    return true;
  };

  (async () => {
    let ok = await doRequest();
    if (!ok) {
      ok = (await refreshAccessToken()) && (await doRequest());
      if (!ok) {
        onError("登录已过期，请重新登录");
      }
    }
  })().catch((err) => {
    if (err.name !== "AbortError") {
      onError(err.message);
    }
  });

  return controller;
}

export const chatApi = {
  getConversations: (page = 1, pageSize = 50) =>
    api
      .get<PaginatedResponse<Conversation>>("/conversations", {
        params: { page, page_size: pageSize },
      })
      .then((r) => r.data),

  getConversation: (id: string) =>
    api.get<ConversationDetail>(`/conversations/${id}`).then((r) => r.data),

  deleteConversation: (id: string) =>
    api.delete(`/conversations/${id}`).then((r) => r.data),

  updateConversation: (id: string, data: { title?: string; status?: string }) =>
    api.patch(`/conversations/${id}`, data).then((r) => r.data),

  /** 一键清空全部历史对话（软删除，数据保留） */
  clearAllConversations: () =>
    api
      .post<{ deleted: number; skipped: number; message: string }>(
        "/conversations/clear"
      )
      .then((r) => r.data),

  /** 欢迎页快捷问题（后台可配置；接口失败时前端回退静态默认值） */
  getQuickQuestions: () =>
    api
      .get<{ questions: string[] }>("/chat/quick-questions")
      .then((r) => r.data.questions),

  /** 提交/修改评价（幂等 upsert） */
  submitFeedback: (
    messageId: string,
    rating: "like" | "dislike",
    comment?: string | null
  ) =>
    api
      .put<{ rating: "like" | "dislike"; comment: string | null }>(
        `/messages/${messageId}/feedback`,
        { rating, comment: comment ?? null }
      )
      .then((r) => r.data),

  /** 取消评价 */
  deleteFeedback: (messageId: string) =>
    api.delete(`/messages/${messageId}/feedback`).then((r) => r.data),

  /** SSE 流式对话 */
  streamChat: (
    message: string,
    conversationId: string | null,
    role: "user" | "admin",
    onEvent: (event: SSEEvent) => void,
    onError: (error: string) => void,
    onDone: () => void
  ): AbortController =>
    streamSSE(
      "/chat/stream",
      {
        conversation_id: conversationId,
        message,
        stream: true,
        role,
      },
      onEvent,
      onError,
      onDone
    ),

  /** SSE 重新生成最后一条回答（事件序列与 streamChat 一致） */
  regenerate: (
    conversationId: string,
    role: "user" | "admin",
    onEvent: (event: SSEEvent) => void,
    onError: (error: string) => void,
    onDone: () => void
  ): AbortController =>
    streamSSE(
      "/chat/regenerate",
      { conversation_id: conversationId, role },
      onEvent,
      onError,
      onDone
    ),
};
