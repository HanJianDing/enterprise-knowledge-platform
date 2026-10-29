export type UserRole = "admin" | "editor" | "user" | "auditor";

export interface UserInfo {
  id: string;
  username: string;
  email: string;
  role: UserRole;
  is_active: boolean;
  /** 首登强制改密：管理员建号/批量导入的账号为 true（企业改造阶段3） */
  must_change_password?: boolean;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  must_change_password?: boolean;
}

export interface ChangePasswordRequest {
  old_password: string;
  new_password: string;
}
