import { describe, it, expect, beforeEach } from "vitest";
import { useAuthStore } from "../stores/authStore";
import { useChatStore } from "../stores/chatStore";

describe("authStore", () => {
  beforeEach(() => {
    // 重置 store
    const store = useAuthStore.getState();
    store.logout();
    localStorage.clear();
  });

  it("初始状态为未认证", () => {
    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(false);
    expect(state.isAdmin).toBe(false);
    expect(state.user).toBeNull();
    expect(state.accessToken).toBeNull();
    expect(state.refreshToken).toBeNull();
  });

  it("login 后设置 token 和认证状态", () => {
    useAuthStore.getState().login({
      access_token: "test_access",
      refresh_token: "test_refresh",
      token_type: "bearer",
    });

    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(true);
    expect(state.accessToken).toBe("test_access");
    expect(state.refreshToken).toBe("test_refresh");
    expect(localStorage.getItem("access_token")).toBe("test_access");
    expect(localStorage.getItem("refresh_token")).toBe("test_refresh");
  });

  it("setUser 后设置用户和 admin 状态", () => {
    useAuthStore.getState().setUser({
      id: "1",
      username: "admin",
      email: "admin@test.com",
      role: "admin",
      is_active: true,
    });

    const state = useAuthStore.getState();
    expect(state.user?.username).toBe("admin");
    expect(state.isAdmin).toBe(true);
  });

  it("普通用户 isAdmin 为 false", () => {
    useAuthStore.getState().setUser({
      id: "2",
      username: "user",
      email: "user@test.com",
      role: "user",
      is_active: true,
    });

    expect(useAuthStore.getState().isAdmin).toBe(false);
  });

  it("员工角色 isAdmin 为 false（企业改造阶段2：客服角色已移除）", () => {
    useAuthStore.getState().setUser({
      id: "3",
      username: "staff",
      email: "staff@test.com",
      role: "user",
      is_active: true,
    });

    const state = useAuthStore.getState();
    expect(state.isAdmin).toBe(false);
  });

  it("logout 后清除所有状态", () => {
    // 先登录
    useAuthStore.getState().login({
      access_token: "test_access",
      refresh_token: "test_refresh",
      token_type: "bearer",
    });
    useAuthStore.getState().setUser({
      id: "1",
      username: "admin",
      email: "admin@test.com",
      role: "admin",
      is_active: true,
    });

    // 退出
    useAuthStore.getState().logout();

    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(false);
    expect(state.isAdmin).toBe(false);
    expect(state.user).toBeNull();
    expect(state.accessToken).toBeNull();
    expect(state.refreshToken).toBeNull();
    expect(localStorage.getItem("access_token")).toBeNull();
    expect(localStorage.getItem("refresh_token")).toBeNull();
  });

  it("setTokens 更新 token", () => {
    useAuthStore.getState().setTokens("new_access", "new_refresh");

    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(true);
    expect(state.accessToken).toBe("new_access");
    expect(state.refreshToken).toBe("new_refresh");
  });

  it("logout 清空聊天残留（跨账号隐私修复）", () => {
    // 造聊天残留
    useChatStore.setState({
      conversations: [
        {
          id: "c1",
          title: "上个账号的会话",
          status: "active",
          message_count: 2,
          created_at: "2026-09-03T00:00:00Z",
          updated_at: "2026-09-03T00:00:00Z",
        },
      ],
      currentConversationId: "c1",
      messages: [
        {
          id: "m1",
          conversation_id: "c1",
          role: "user",
          content: "隐私聊天内容",
          created_at: "2026-09-03T00:00:00Z",
        },
      ],
      isStreaming: true,
      streamContent: "流式残留",
    });

    useAuthStore.getState().logout();

    const chat = useChatStore.getState();
    expect(chat.conversations).toEqual([]);
    expect(chat.currentConversationId).toBeNull();
    expect(chat.messages).toEqual([]);
    expect(chat.isStreaming).toBe(false);
    expect(chat.streamContent).toBe("");
  });
});

describe("authStore 顶号提示", () => {
  beforeEach(() => {
    useAuthStore.getState().logout();
    useAuthStore.getState().clearKickedMessage();
    localStorage.clear();
  });

  it("setKickedMessage 写入提示", () => {
    useAuthStore.getState().setKickedMessage("账号已在其他设备登录，请重新登录");
    expect(useAuthStore.getState().kickedMessage).toBe("账号已在其他设备登录，请重新登录");
  });

  it("logout 不清除顶号提示（登录页需要展示），clearKickedMessage 清除", () => {
    useAuthStore.getState().setKickedMessage("账号已在其他设备登录，请重新登录");
    useAuthStore.getState().logout();
    expect(useAuthStore.getState().kickedMessage).toBe("账号已在其他设备登录，请重新登录");
    useAuthStore.getState().clearKickedMessage();
    expect(useAuthStore.getState().kickedMessage).toBeNull();
  });
});
