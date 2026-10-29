import { describe, it, expect, vi, beforeEach } from "vitest";
import { useAuthStore } from "../stores/authStore";

const mockPost = vi.fn();

// mock 裸 axios：api.ts 的实例拦截器与裸 axios.post 都来自同一模块
vi.mock("axios", () => ({
  default: {
    create: () => ({
      interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
      get: vi.fn(),
      post: vi.fn(),
    }),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

import { refreshAccessToken } from "../services/api";

describe("refreshAccessToken 单飞刷新", () => {
  beforeEach(() => {
    mockPost.mockClear();
    localStorage.clear();
    useAuthStore.getState().logout();
    useAuthStore.getState().clearKickedMessage();
  });

  it("刷新成功：更新 store 并返回 true", async () => {
    localStorage.setItem("refresh_token", "r1");
    useAuthStore.setState({ refreshToken: "r1" });
    mockPost.mockResolvedValue({ data: { access_token: "new_access" } });

    const ok = await refreshAccessToken();
    expect(ok).toBe(true);
    expect(useAuthStore.getState().accessToken).toBe("new_access");
    expect(localStorage.getItem("access_token")).toBe("new_access");
  });

  it("并发调用共享同一次刷新请求（防刷新风暴）", async () => {
    localStorage.setItem("refresh_token", "r1");
    useAuthStore.setState({ refreshToken: "r1" });
    let resolvePost: (v: unknown) => void = () => {};
    mockPost.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePost = resolve;
        })
    );

    const p1 = refreshAccessToken();
    const p2 = refreshAccessToken();
    const p3 = refreshAccessToken();

    // 三路并发只发了一次刷新请求
    expect(mockPost).toHaveBeenCalledTimes(1);
    resolvePost({ data: { access_token: "new_access" } });
    const results = await Promise.all([p1, p2, p3]);
    expect(results).toEqual([true, true, true]);
  });

  it("刷新失败且含顶号文案：记录踢出提示并登出", async () => {
    localStorage.setItem("refresh_token", "r1");
    localStorage.setItem("access_token", "a1");
    useAuthStore.setState({ refreshToken: "r1", accessToken: "a1" });
    mockPost.mockRejectedValue({
      response: { data: { detail: "账号已在其他设备登录，请重新登录" } },
    });

    const ok = await refreshAccessToken();
    expect(ok).toBe(false);
    expect(useAuthStore.getState().kickedMessage).toContain("其他设备");
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(localStorage.getItem("access_token")).toBeNull();
  });

  it("无 refresh token：直接登出并返回 false", async () => {
    const ok = await refreshAccessToken();
    expect(ok).toBe(false);
    expect(mockPost).not.toHaveBeenCalled();
  });
});
