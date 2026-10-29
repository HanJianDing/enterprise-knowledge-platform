export interface DocumentInfo {
  id: string;
  filename: string;
  original_name: string;
  file_type: string;
  file_size: number;
  status: "uploaded" | "processing" | "completed" | "failed";
  chunk_count: number;
  content_type: string;
  /** 企业知识库阶段1：文档归属知识库 */
  kb_id?: string;
  kb_name?: string;
  category_id?: string;
  category_name?: string;
  created_at: string;
  updated_at: string;
  /** 解析事件流水（产品化 A3）；历史旧数据无此字段 */
  process_log?: ProcessLogEvent[];
  /** 处理失败原因（解析流水弹窗顶部展示） */
  error_message?: string | null;
  /** 批次5 7b 版本管理：版本号（1=首版）与上一版本 id（null=首版） */
  version_no: number;
  version_of: string | null;
}

/** 解析流水事件（后端 documents.process_log JSON 数组元素） */
export interface ProcessLogEvent {
  time: string;
  stage: string;
  detail: string;
}

export interface KnowledgeCategory {
  id: string;
  name: string;
  description?: string;
  parent_id?: string;
  /** 企业知识库阶段1：分类归属知识库 */
  kb_id?: string;
  children: KnowledgeCategory[];
  created_at: string;
}

export interface ChunkInfo {
  chunk_id: string;
  content: string;
  chunk_index: number;
  chunk_hash: string;
  filename: string;
  category_id: string;
  parent_category_id?: string;
  /** 分块模式：structured / faq / long_text / default（后端新返回字段） */
  chunk_type?: string;
}

export interface ChunkListResponse {
  document_id: string;
  total: number;
  chunks: ChunkInfo[];
}

export interface KnowledgeStats {
  total_documents: number;
  total_chunks: number;
  completed_documents: number;
  processing_documents: number;
  failed_documents: number;
  total_size_bytes: number;
  categories_count: number;
  /** 指定了 kb 参数时为该库统计 */
  kb_id?: string;
}
