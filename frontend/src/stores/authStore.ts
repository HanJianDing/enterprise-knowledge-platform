import { create } from "zustand";
import type { UserInfo, TokenResponse } from "../types/user";
import { useChatStore } from "./chatStore";

interface AuthState {
  user: UserInfo | null;
  accessToken: string | null;
  refreshToken: string | null;
  isAuthenticated: boolean;
  isAdmin: boolean;
  /** 审计视图权限：admin 或 auditor（产品化 C5：审计员只读审计） */
  isAuditViewer: boolean;
  // 顶号提示：被其他设备踢下线时写入，登录页展示一次后清除。
  // logout 不清除它（否则提示先被抹掉）；由展示方调用 clearKickedMessage。
  kickedMessage: string | null;

  setUser: (user: UserInfo) => void;
  setTokens: (accessToken: string, refreshToken: string) => void;
  login: (data: TokenResponse) => void;
  logout: () => void;
  setKickedMessage: (msg: string) => void;
  clearKickedMessage: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  accessToken: localStorage.getItem("access_token"),
  refreshToken: localStorage.getItem("refresh_token"),
  isAuthenticated: !!localStorage.getItem("access_token"),
  isAdmin: false,
  isAuditViewer: false,
  kickedMessage: null,

  setUser: (user) =>
    set({
      user,
      isAdmin: user.role === "admin",
      isAuditViewer: user.role === "admin" || user.role === "auditor",
    }),

  setTokens: (accessToken, refreshToken) => {
    localStorage.setItem("access_token", accessToken);
    localStorage.setItem("refresh_token", refreshToken);
    set({ accessToken, refreshToken, isAuthenticated: true });
  },

  login: (data: TokenResponse) => {
    localStorage.setItem("access_token", data.access_token);
    localStorage.setItem("refresh_token", data.refresh_token);
    set({
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
      isAuthenticated: true,
    });
  },

  logout: () => {
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    set({
      user: null,
      accessToken: null,
      refreshToken: null,
      isAuthenticated: false,
      isAdmin: false,
      isAuditViewer: false,
      // 注意：不清 kickedMessage，登录页还要靠它弹提示
    });
    // 上线安全修复：清空聊天内存残留——共用电脑上换账号登录时，
    // 不能看到上一个账号的会话列表与聊天记录
    useChatStore.getState().resetChat();
  },

  setKickedMessage: (msg) => set({ kickedMessage: msg }),
  clearKickedMessage: () => set({ kickedMessage: null }),
}));
