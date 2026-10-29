import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi } from "vitest";
import LoginPage from "../pages/auth/LoginPage";

// Mock the API and store
vi.mock("../services/authApi", () => ({
  authApi: {
    login: vi.fn(),
    getMe: vi.fn(),
    // 企业知识库改造阶段4：SSO 入口（未配置 provider 时返回空列表）
    getSsoProviders: vi.fn().mockResolvedValue([]),
    getSsoUrl: vi.fn(),
  },
}));

vi.mock("../stores/authStore", () => ({
  useAuthStore: vi.fn((selector?: any) => {
    const state = {
      login: vi.fn(),
      setUser: vi.fn(),
      isAuthenticated: false,
      accessToken: null,
    };
    return selector ? selector(state) : state;
  }),
}));

describe("LoginPage", () => {
  const renderLogin = () => {
    return render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>
    );
  };

  it("应该渲染登录表单", () => {
    renderLogin();
    expect(screen.getByText("企业知识库")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("用户名")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("密码")).toBeInTheDocument();
    expect(screen.getByText("登 录")).toBeInTheDocument();
  });

  it("不显示注册入口（企业改造阶段3：开放注册已关闭）", () => {
    renderLogin();
    expect(screen.queryByText("注册账号")).toBeNull();
  });

  it("不应该显示管理员凭据", () => {
    renderLogin();
    expect(screen.queryByText(/admin/)).toBeNull();
    expect(screen.queryByText(/123456/)).toBeNull();
  });

  it("用户名输入框存在", () => {
    renderLogin();
    const usernameInput = screen.getByPlaceholderText("用户名");
    expect(usernameInput).toBeInTheDocument();
  });

  it("密码输入框存在", () => {
    renderLogin();
    const passwordInput = screen.getByPlaceholderText("密码");
    expect(passwordInput).toBeInTheDocument();
  });
});
