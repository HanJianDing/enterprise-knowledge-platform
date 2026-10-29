export interface Conversation {
  id: string;
  title: string;
  status: "active" | "archived";
  message_count: number;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  // agent=客服时代的历史人工回复（企业改造阶段2后不再产生，仅历史数据保留展示）
  role: "user" | "assistant" | "system" | "agent";
  content: string;
  citations?: Citation[];
  token_count?: number;
  latency_ms?: number;
  /** 当前用户对该消息的评价（未评价为 undefined） */
  feedback?: FeedbackInfo;
  created_at: string;
  // 仅回退路径填充（后端不持久化调试信息）
  stage_times?: StageTimes;
  /** 可观测 trace_id（批次4）：Jaeger 跳转定位本次问答链路 */
  trace_id?: string;
  token_usage?: TokenUsage;
}

export interface FeedbackInfo {
  rating: "like" | "dislike";
  comment?: string | null;
}

export interface Citation {
  index: number;
  doc_id: string;
  title: string;
  text: string;
  category_path?: string; // "父分类 > 子分类"，旧消息可能缺失
  /** 该切片在文档中的序号（引用"查看上下文"定位用；历史旧消息可能缺失） */
  chunk_idx?: number;
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[];
}

export interface ChatRequest {
  conversation_id?: string | null;
  message: string;
  stream: boolean;
  role?: "user" | "admin";
}

export interface StageTimes {
  classify_ms?: number;
  retrieve_ms?: number;
  generate_ms?: number;
  total_ms?: number;
}

export interface TokenUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
}

export interface SSEEvent {
  type: string;
  message?: string;
  documents?: SSESource[];
  content?: string;
  conversation_id?: string;
  message_id?: string;
  latency_ms?: number;
  token_count?: number;
  /** 可观测 trace_id（批次4）：Jaeger 跳转定位本次问答链路 */
  trace_id?: string;
  category_id?: string;
  category_name?: string;
  clarified_question?: string;
  role?: "user" | "admin";
  truncated?: boolean;
  stage_times?: StageTimes;
  token_usage?: TokenUsage;
}

export interface SSESource {
  index: number;
  doc_id: string;
  title: string;
  chunk_idx: number;
  text: string;
  category_path?: string; // "父分类 > 子分类"
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}
