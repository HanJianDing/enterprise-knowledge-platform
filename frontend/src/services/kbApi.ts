/** 知识库/部门/权限 API（企业知识库改造 阶段1） */
import api from "./api";
import type {
  KnowledgeBaseInfo, KbMemberItem, KbMemberInput, DepartmentNode,
} from "../types/kb";

export const kbApi = {
  // ── 知识库 ──

  listKbs: () =>
    api.get<KnowledgeBaseInfo[]>("/admin/knowledge-bases").then((r) => r.data),

  createKb: (data: { name: string; description?: string; is_public?: boolean }) =>
    api.post<KnowledgeBaseInfo>("/admin/knowledge-bases", data).then((r) => r.data),

  updateKb: (
    kbId: string,
    data: { name?: string; description?: string; is_public?: boolean }
  ) => api.patch(`/admin/knowledge-bases/${kbId}`, data).then((r) => r.data),

  deleteKb: (kbId: string) =>
    api.delete(`/admin/knowledge-bases/${kbId}`).then((r) => r.data),

  // ── 库授权 ──

  getMembers: (kbId: string) =>
    api.get<KbMemberItem[]>(`/admin/knowledge-bases/${kbId}/members`).then((r) => r.data),

  setMembers: (kbId: string, members: KbMemberInput[]) =>
    api.put(`/admin/knowledge-bases/${kbId}/members`, { members }).then((r) => r.data),

  // ── 部门 ──

  getDepartments: () =>
    api.get<DepartmentNode[]>("/admin/departments").then((r) => r.data),

  createDepartment: (data: { name: string; parent_id?: string | null }) =>
    api.post<DepartmentNode>("/admin/departments", data).then((r) => r.data),

  updateDepartment: (deptId: string, data: { name?: string; parent_id?: string | null }) =>
    api.patch(`/admin/departments/${deptId}`, data).then((r) => r.data),

  deleteDepartment: (deptId: string) =>
    api.delete(`/admin/departments/${deptId}`).then((r) => r.data),

  // ── 用户-部门绑定 ──

  getUserDepartments: (userId: string) =>
    api.get<DepartmentNode[]>(`/admin/users/${userId}/departments`).then((r) => r.data),

  setUserDepartments: (userId: string, departmentIds: string[]) =>
    api.put(`/admin/users/${userId}/departments`, { department_ids: departmentIds })
      .then((r) => r.data),
};
