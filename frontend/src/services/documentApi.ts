import api from "./api";
import type { PaginatedResponse } from "../types/chat";
import type {
  DocumentInfo,
  KnowledgeCategory,
  KnowledgeStats,
  ChunkListResponse,
} from "../types/document";

export const documentApi = {
  upload: (file: File, kbId: string, categoryId?: string, contentType?: string) => {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("kb_id", kbId);
    if (categoryId) formData.append("category_id", categoryId);
    if (contentType) formData.append("content_type", contentType);
    return api
      .post<{ id: string; status: string; message: string }>(
        "/knowledge/documents/upload",
        formData,
        {
          headers: { "Content-Type": "multipart/form-data" },
          timeout: 120000,
        }
      )
      .then((r) => r.data);
  },

  list: (params: {
    page?: number;
    page_size?: number;
    status?: string;
    search?: string;
    /** 企业知识库阶段1：指定知识库 */
    kb_id?: string;
    /** 批次F 验收 3.16：分类筛选（主题父分类含全部形态子分类文档） */
    category_id?: string;
  }) =>
    api
      .get<PaginatedResponse<DocumentInfo>>("/knowledge/documents", { params })
      .then((r) => r.data),

  delete: (id: string) =>
    api.delete(`/knowledge/documents/${id}`).then((r) => r.data),

  /** 重试处理失败的文档（企业知识库改造阶段6）；force=true 对已完成文档按当前模板重新分块（产品化 A1） */
  /** 版本链（批次5 7b）：旧版在前新版在后 */
  getVersions: (id: string) =>
    api
      .get<{ id: string; version_no: number; status: string; created_at: string | null; chunk_count: number }[]>(
        `/admin/documents/${id}/versions`,
      )
      .then((r) => r.data),

  /** 版本回溯（批次5 7b）：旧版恢复为当前版本 */
  restoreVersion: (id: string) =>
    api
      .post<{ restored_id: string; version_no: number }>(
        `/admin/documents/${id}/restore`,
      )
      .then((r) => r.data),

  /** 指定版本纯文本内容（批次F 前置·diff 视图数据源） */
  getVersionContent: (id: string) =>
    api
      .get<{
        doc_id: string;
        version_no: number;
        filename: string;
        content_type: string;
        content: string;
      }>(`/admin/documents/${id}/content`)
      .then((r) => r.data),

  reprocess: (id: string, force?: boolean) =>
    api
      .post(`/knowledge/documents/${id}/reprocess`, null, {
        params: force ? { force: true } : undefined,
      })
      .then((r) => r.data),

  getCategories: (kbId?: string) =>
    api
      .get<KnowledgeCategory[]>("/knowledge/categories", {
        params: kbId ? { kb_id: kbId } : undefined,
      })
      .then((r) => r.data),

  createCategory: (data: {
    name: string;
    description?: string;
    parent_id?: string;
    kb_id: string;
  }) => api.post<KnowledgeCategory>("/knowledge/categories", data).then((r) => r.data),

  deleteCategory: (id: string) =>
    api.delete(`/knowledge/categories/${id}`).then((r) => r.data),

  getStats: (kbId?: string) =>
    api
      .get<KnowledgeStats>("/knowledge/stats", {
        params: kbId ? { kb_id: kbId } : undefined,
      })
      .then((r) => r.data),

  getChunks: (docId: string, around?: number, radius?: number) =>
    api
      .get<ChunkListResponse>(`/knowledge/documents/${docId}/chunks`, {
        params:
          around !== undefined
            ? { around, radius: radius ?? 1 }
            : undefined,
      })
      .then((r) => r.data),
};
