import api from "./api";
import type { LoginRequest, TokenResponse, UserInfo, ChangePasswordRequest } from "../types/user";

export const authApi = {
  login: (data: LoginRequest) =>
    api.post<TokenResponse>("/auth/login", data).then((r) => r.data),

  getMe: () => api.get<UserInfo>("/auth/me").then((r) => r.data),

  changePassword: (data: ChangePasswordRequest) =>
    api.post<{ message: string }>("/auth/change-password", data).then((r) => r.data),

  refresh: (refreshToken: string) =>
    api.post<TokenResponse>("/auth/refresh", { refresh_token: refreshToken }).then(
      (r) => r.data
    ),

  /** SSO 已启用的登录方式（企业知识库改造阶段4；未配置的 provider 不出现） */
  getSsoProviders: () =>
    api
      .get<{ providers: { name: string }[] }>("/auth/sso/providers")
      .then((r) => r.data.providers.map((p) => p.name)),

  /** 生成某个 provider 的授权链接（state 后端已签名防 CSRF） */
  getSsoUrl: (provider: string) =>
    api.get<{ auth_url: string }>(`/auth/sso/${provider}/url`).then((r) => r.data),
};
