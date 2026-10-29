import api from "./api";
import type { UserInfo, UserRole } from "../types/user";
import type { PaginatedResponse } from "../types/chat";

export interface DashboardData {
  user_count: number;
  conversation_count: number;
  message_count: number;
  document_count: number;
  documents_completed: number;
  documents_processing: number;
  documents_failed: number;
}

export interface FeedbackStats {
  like: number;
  dislike: number;
  total: number;
  satisfaction_rate: number | null; // 无评价时为 null
}

export interface DashboardStats {
  trend: { date: string; conversations: number; messages: number }[];
  feedback: FeedbackStats;
  quality: { avg_latency_ms: number | null; avg_tokens: number | null };
  categories: { category_id: string | null; name: string; count: number }[];
}

export interface FeedbackItem {
  id: string;
  rating: "like" | "dislike";
  comment: string | null;
  created_at: string;
  username: string;
  conversation_title: string;
  message_content: string;
}

export interface AuditLogItem {
  id: string;
  username: string;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  detail: Record<string, unknown> | null;
  ip: string | null;
  user_agent: string | null;
  created_at: string;
}

export interface UserImportError {
  row: number;
  username: string;
  reason: string;
}

export const adminApi = {
  getDashboard: () =>
    api.get<DashboardData>("/admin/dashboard").then((r) => r.data),

  getUsers: (page = 1, pageSize = 20, search?: string) =>
    api
      .get<PaginatedResponse<UserInfo>>("/admin/users", {
        params: { page, page_size: pageSize, search: search || undefined },
      })
      .then((r) => r.data),

  /** 管理员创建员工账号（初始密码首登强制修改，企业改造阶段3） */
  createUser: (data: {
    username: string;
    email: string;
    password: string;
    role: UserRole;
    department_ids?: string[];
  }) => api.post<UserInfo>("/admin/users", data).then((r) => r.data),

  /** CSV 批量导入员工账号 */
  importUsers: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return api
      .post<{ created: number; errors: UserImportError[] }>(
        "/admin/users/import",
        formData,
        { headers: { "Content-Type": "multipart/form-data" }, timeout: 60000 }
      )
      .then((r) => r.data);
  },

  /** 审计日志查询（企业改造阶段3；产品化 C5：admin/auditor 均可访问） */
  getAuditLogs: (params: {
    page?: number;
    page_size?: number;
    action?: string;
    username?: string;
    resource_type?: string;
  }) =>
    api
      .get<PaginatedResponse<AuditLogItem>>("/admin/audit-logs", { params })
      .then((r) => r.data),

  /** 审计日志导出 CSV（产品化 C3：内审/回溯需要可导出的证据文件） */
  exportAuditLogs: (params: {
    action?: string;
    username?: string;
    resource_type?: string;
  }) =>
    api
      .get("/admin/audit-logs/export", {
        params,
        responseType: "blob",
        timeout: 120000,
      })
      .then((r) => r.data as Blob),

  /** 管理员重置员工密码（产品化 C2：重置后首登强制改密） */
  resetPassword: (userId: string, newPassword: string) =>
    api
      .post<{ message: string }>(`/admin/users/${userId}/reset-password`, {
        new_password: newPassword,
      })
      .then((r) => r.data),

  changeRole: (userId: string, role: UserRole) =>
    api
      .patch(`/admin/users/${userId}/role`, null, { params: { role } })
      .then((r) => r.data),

  changeStatus: (userId: string, isActive: boolean) =>
    api
      .patch(`/admin/users/${userId}/status`, null, {
        params: { is_active: isActive },
      })
      .then((r) => r.data),

  // ── 欢迎页快捷问题 ──

  getQuickQuestions: () =>
    api.get<{ questions: string[] }>("/chat/quick-questions").then((r) => r.data),

  setQuickQuestions: (questions: string[]) =>
    api
      .put("/admin/settings/quick-questions", { questions })
      .then((r) => r.data),

  // ── 满意度反馈 ──

  getFeedbackStats: () =>
    api.get<FeedbackStats>("/admin/feedback/stats").then((r) => r.data),

  getFeedbackList: (params: {
    page?: number;
    page_size?: number;
    rating?: "like" | "dislike";
    has_comment?: boolean;
  }) =>
    api
      .get<{ items: FeedbackItem[]; total: number; page: number; page_size: number }>(
        "/admin/feedback",
        { params }
      )
      .then((r) => r.data),

  // ── 图表看板 ──

  getDashboardStats: (days = 30) =>
    api
      .get<DashboardStats>("/admin/dashboard/stats", { params: { days } })
      .then((r) => r.data),

  // ── 检索评估（产品化 B4） ──

  /** 试检索：输入问题即时查看混合检索命中切片 */
  evalRetrievalTest: (question: string, topK = 6, withRerank = false) =>
    api
      .post<RetrievalTestResult>("/admin/eval/retrieval-test", {
        question,
        top_k: topK,
        with_rerank: withRerank,
      })
      .then((r) => r.data),

  /** 评估集跑分（约 15 秒，接口超时放宽；evalSet=all/regression/long 分层，验收批次1） */
  runEval: (withRerank = false, evalSet = "all") =>
    api
      .post<EvalRunResult>(
        "/admin/eval/run",
        { with_rerank: withRerank, eval_set: evalSet },
        { timeout: 300000 },
      )
      .then((r) => r.data),

  /** API key 管理（批次11e OpenAI 兼容 API；批次A 补 expires_at 管理入口；
   *  批次F 挂清单 #12：kbIds 库白名单（不选=穿透账号权限）） */
  createApiKey: (name: string, expiresAt?: string | null, kbIds?: string[] | null) =>
    api
      .post<{ id: string; key: string; name: string; prefix: string;
              created_at: string; expires_at: string | null; kb_ids: string[] | null }>(
        "/admin/api-keys",
        { name, expires_at: expiresAt || null, kb_ids: kbIds || null },
      )
      .then((r) => r.data),
  apiKeyList: () =>
    api
      .get<ApiKeyItem[]>("/admin/api-keys")
      .then((r) => r.data),
  revokeApiKey: (keyId: string) =>
    api
      .patch<{ message: string }>(`/admin/api-keys/${keyId}/revoke`)
      .then((r) => r.data),
  setApiKeyExpiry: (keyId: string, expiresAt: string | null) =>
    api
      .patch<{ message: string; expires_at: string | null }>(
        `/admin/api-keys/${keyId}/expiry`,
        { expires_at: expiresAt },
      )
      .then((r) => r.data),

  /** MCP Server 工具管理（批次F 挂清单 #13 页面化） */
  mcpServerList: () =>
    api.get<McpServerItem[]>("/admin/mcp-servers").then((r) => r.data),
  mcpServerCreate: (data: McpServerInput) =>
    api.post<McpServerItem>("/admin/mcp-servers", data).then((r) => r.data),
  mcpServerDelete: (serverId: string) =>
    api.delete(`/admin/mcp-servers/${serverId}`).then((r) => r.data),
  mcpServerToggle: (serverId: string, enabled: boolean) =>
    api
      .patch<McpServerItem>(`/admin/mcp-servers/${serverId}/toggle`, { enabled })
      .then((r) => r.data),
  mcpServerTest: (data: McpServerInput) =>
    api
      .post<{ ok: boolean; tools: { name: string; description: string }[]; error?: string }>(
        "/admin/mcp-servers/test", data,
      )
      .then((r) => r.data),

  /** RAPTOR 摘要树概览（批次B 可视化页） */
  raptorOverview: () =>
    api
      .get<RaptorOverview>("/admin/raptor/overview")
      .then((r) => r.data),

  /** 审批规则列表（批次2-5 规则管理页）：解析产物可视化校验 */
  approvalRules: (approver?: string) =>
    api
      .get<ApprovalRuleItem[]>("/admin/approval-rules", { params: { approver } })
      .then((r) => r.data),

  /** 规则冲突列表（批次3）：旧版/新版制度并存告警 */
  approvalRuleConflicts: () =>
    api.get<ApprovalRuleConflict[]>("/admin/approval-rules/conflicts").then((r) => r.data),

  /** 图谱概览（批次2-3）：节点/边/社区报告（可视化页数据源） */
  graphOverview: () =>
    api.get<GraphOverviewResult>("/admin/graph/overview").then((r) => r.data),

  /** 手动触发社区检测（批次2-3）：Leiden 分区 + LLM 社区报告 */
  refreshGraphCommunities: () =>
    api
      .post<{ partition: Record<string, number>; community_count: number }>(
        "/admin/graph/communities/refresh",
      )
      .then((r) => r.data),

  /** 检索调优配置（验收批次1·1a）：同义词表 + 词权重 */
  suggestRetrievalTerms: () =>
    api
      .post<{ suggestions: { query_term: string; doc_term: string; reason: string }[]; message: string }>(
        "/admin/retrieval-terms/suggest",
      )
      .then((r) => r.data),
  getRetrievalSuggestions: () =>
    api
      .get<{ suggestions: { query_term: string; doc_term: string; reason: string }[] }>(
        "/admin/retrieval-terms/suggestions",
      )
      .then((r) => r.data),
  adoptRetrievalTerm: (queryTerm: string, docTerm: string) =>
    api
      .post<{ message: string }>("/admin/retrieval-terms/adopt", {
        query_term: queryTerm, doc_term: docTerm,
      })
      .then((r) => r.data),
  ignoreRetrievalTerm: (queryTerm: string, docTerm: string) =>
    api
      .post<{ message: string }>("/admin/retrieval-terms/ignore-suggestion", {
        query_term: queryTerm, doc_term: docTerm,
      })
      .then((r) => r.data),
  getRetrievalTerms: () =>
    api
      .get<{ synonyms: Record<string, string[]>; weights: Record<string, number> }>("/admin/retrieval-terms")
      .then((r) => r.data),
  saveRetrievalTerms: (data: { synonyms: Record<string, string[]>; weights: Record<string, number> }) =>
    api
      .put<{ message: string }>("/admin/retrieval-terms", data)
      .then((r) => r.data),

  /** 库级检索配置（验收批次1·1d） */
  getKbRetrievalConfig: (kbId: string) =>
    api
      .get<{ top_k?: number; pagerank_alpha?: number }>(`/admin/knowledge-bases/${kbId}/retrieval-config`)
      .then((r) => r.data),
  saveKbRetrievalConfig: (kbId: string, data: { top_k: number | null; pagerank_alpha: number | null }) =>
    api
      .put<{ message: string }>(`/admin/knowledge-bases/${kbId}/retrieval-config`, data)
      .then((r) => r.data),
};

export interface RetrievalTestDoc {
  index: number;
  filename: string;
  chunk_index: number;
  content: string;
  content_preview: string;
}

export interface RetrievalTestResult {
  query: string;
  top_k: number;
  reranked: boolean;
  docs: RetrievalTestDoc[];
}

export interface EvalGroupResult {
  n: number;
  hit_rate: number;
  mrr: number;
  avg_ms: number;
  details: {
    question: string;
    expected: string[];
    hit: boolean;
    rank: number | null;
    got: string[];
  }[];
}

export interface McpCredentialItem {
  id: string;
  name: string;
  owner_username: string;
  kb_ids: string[] | null;
  revoked: boolean;
  created_at: string | null;
  revoked_at: string | null;
}

// 批次D：外部接入凭证管理
export const mcpCredentialApi = {
  list: () =>
    api.get<{ items: McpCredentialItem[] }>("/admin/mcp-credentials").then((r) => r.data),
  create: (data: { name: string; kb_ids?: string[] | null }) =>
    api
      .post<{ id: string; name: string; token: string; kb_ids: string[] | null; message: string }>(
        "/admin/mcp-credentials",
        data
      )
      .then((r) => r.data),
  revoke: (id: string) =>
    api.post<{ message: string }>(`/admin/mcp-credentials/${id}/revoke`).then((r) => r.data),
};

/** 文档命中口径结果（all/regression/long/authority 层） */
export interface EvalDocRunResult {
  total_questions: number;
  doc_count: number;
  with_rerank: boolean;
  eval_set: string;
  elapsed_ms: number;
  standard: EvalGroupResult;
  fuzzy: EvalGroupResult;
  total: { n: number; hit_rate: number; mrr: number; avg_ms: number };
}

/** 审批规则层结果（批次2-4，eval_set=approval）：规则引擎确定性匹配 */
export interface EvalApprovalRunResult {
  total_questions: number;
  doc_count: number;
  with_rerank: boolean;
  eval_set: string;
  elapsed_ms: number;
  approval: {
    n: number;
    accuracy: number;
    details: {
      question: string;
      hit: boolean;
      expected: string;
      got: string | null;
    }[];
  };
}

/** 关系层结果（批次2-2，eval_set=relation）：图谱链路分环节指标，
 * 后端不返回 standard/fuzzy/total（质检 G3：两套响应不能混成一份类型） */
export interface EvalRelationRunResult {
  total_questions: number;
  doc_count: number;
  with_rerank: boolean;
  eval_set: string;
  elapsed_ms: number;
  relation: {
    n: number;
    graph_available: boolean;
    intervention_rate: number;
    entity_link_accuracy: number;
    path_accuracy: number;
    neighbor_accuracy: number;
    no_intervention_accuracy: number;
    details: {
      question: string;
      kind: string;
      result: {
        entities: string[];
        path_steps: string[];
        neighbors: string[];
      } | null;
    }[];
  };
}

/** 判别联合：有 relation/approval 字段=专项层响应，否则=文档命中口径响应 */
export type EvalRunResult = EvalDocRunResult | EvalRelationRunResult | EvalApprovalRunResult;

/** 规则冲突对（批次3/3.5）：旧版/新版制度并存告警 + 重叠段描述 */
export interface ApprovalRuleConflict {
  business_type: string;
  unit: string;
  /** 重叠段文本（"5000元以上"/"3000 至 8000元"）——批次3.5 部分重叠检测 */
  overlap: string;
  rule_a: { source_text: string; doc_name: string; approver: string };
  rule_b: { source_text: string; doc_name: string; approver: string };
  resolved_approver: string;
  resolved_doc: string;
}

/** RAPTOR 摘要树概览（批次B 可视化页） */
export interface RaptorOverview {
  documents: Array<{
    doc_id: string;
    filename: string;
    clusters: Array<{
      tree_level: number;
      cluster_id: string;
      child_count: number;
      child_chunk_indices: number[];
      summary_preview: string;
      built_at: string | null;
    }>;
  }>;
}

/** API key 条目（批次11e：明文只生成时返回一次，列表仅前缀；
 * 批次A 补 expires_at 过期时间字段；批次F 挂清单 #12 补 kb_ids 库白名单） */
export interface ApiKeyItem {
  id: string;
  name: string;
  prefix: string;
  is_active: boolean;
  kb_ids: string[] | null;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

/** MCP Server 配置（批次F 挂清单 #13 页面化） */
export interface McpServerItem {
  id: string;
  name: string;
  transport: "http" | "stdio";
  url: string | null;
  command: string | null;
  args: string[];
  enabled: boolean;
  created_at: string;
}

export type McpServerInput =
  | { name: string; transport: "http"; url: string }
  | { name: string; transport: "stdio"; command: string; args: string[] };

/** 审批规则条目（批次2-5 规则管理页） */
export interface ApprovalRuleItem {
  id: string;
  business_type: string;
  min_value: number | null;
  max_value: number | null;
  unit: string;
  approver: string;
  action: string;
  source_text: string;
  source_doc: string;
}

/** 图谱可视化数据（批次2-3） */
export interface GraphOverviewResult {
  nodes: {
    name: string;
    type: string;
    description: string;
    community_id: number | null;
  }[];
  edges: {
    source: string;
    target: string;
    relation: string;
    strength: number;
  }[];
  communities: {
    id: number;
    members: string[];
    summary: string;
    count: number;
  }[];
}
