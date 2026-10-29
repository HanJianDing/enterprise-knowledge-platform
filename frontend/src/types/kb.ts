/** 知识库/部门/权限类型（企业知识库改造 阶段1） */

export interface KnowledgeBaseInfo {
  id: string;
  name: string;
  description?: string | null;
  is_public: boolean;
  created_at: string;
  document_count: number;
  member_count: number;
}

export interface KbMemberItem {
  id: string;
  subject_type: "dept" | "user";
  subject_id: string;
  subject_name: string;
  can_read: boolean;
  can_upload: boolean;
  can_manage: boolean;
  created_at: string;
}

export interface DepartmentNode {
  id: string;
  name: string;
  parent_id?: string | null;
  children: DepartmentNode[];
  created_at: string;
}

/** 前端提交的一条授权（subject_name 仅展示用，提交时后端忽略） */
export interface KbMemberInput {
  subject_type: "dept" | "user";
  subject_id: string;
  can_read: boolean;
  can_upload: boolean;
  can_manage: boolean;
}
