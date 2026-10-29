import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import MainLayout from "../layouts/MainLayout";

// Mock the API（user 已存在时不会触发 getMe）
vi.mock("../services/authApi", () => ({
  authApi: { getMe: vi.fn().mockResolvedValue({}) },
}));

// 可变 mock state：各用例按角色改写 isAdmin
let mockState: {
  user: { id: string; username: string; role: string };
  isAdmin: boolean;
  logout: ReturnType<typeof vi.fn>;
  setUser: ReturnType<typeof vi.fn>;
};

vi.mock("../stores/authStore", () => ({
  useAuthStore: vi.fn(() => mockState),
}));

const renderLayout = (initialPath = "/") =>
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      <MainLayout />
    </MemoryRouter>
  );

describe("MainLayout 侧边导航（角色菜单可见性，企业知识库改造阶段2）", () => {
  beforeEach(() => {
    mockState = {
      user: { id: "u1", username: "测试用户", role: "user" },
      isAdmin: false,
      logout: vi.fn(),
      setUser: vi.fn(),
    };
  });

  it("普通员工只看到知识问答入口", () => {
    renderLayout();
    expect(screen.getByText("知识问答")).toBeInTheDocument();
    expect(screen.queryByText("知识库管理")).toBeNull();
    expect(screen.queryByText("知识库与权限")).toBeNull();
    expect(screen.queryByText("部门管理")).toBeNull();
    expect(screen.queryByText("系统管理")).toBeNull();
    expect(screen.queryByText("用户管理")).toBeNull();
  });

  it("管理员看到全部菜单", () => {
    mockState.isAdmin = true;
    mockState.user.role = "admin";
    renderLayout();
    expect(screen.getByText("知识问答")).toBeInTheDocument();
    expect(screen.getByText("知识库管理")).toBeInTheDocument();
    expect(screen.getByText("知识库与权限")).toBeInTheDocument();
    expect(screen.getByText("部门管理")).toBeInTheDocument();
    expect(screen.getByText("系统管理")).toBeInTheDocument();
    expect(screen.getByText("满意度反馈")).toBeInTheDocument();
    expect(screen.getByText("用户管理")).toBeInTheDocument();
    // 客服相关菜单已随阶段2剥离
    expect(screen.queryByText("客服工作台")).toBeNull();
    expect(screen.queryByText("客服管理")).toBeNull();
  });

  it("侧边栏显示 Logo 与用户名", () => {
    renderLayout();
    expect(screen.getByText("企业知识库")).toBeInTheDocument();
    expect(screen.getByText("测试用户")).toBeInTheDocument();
  });

  it("MCP 工具页路径高亮对应菜单（前缀包含不串台）", () => {
    // 批次F 验收修复回归：/admin/mcp 是 /admin/mcp-tools 的前缀，
    // 此前点 MCP 工具页高亮停在"外部接入"
    mockState.isAdmin = true;
    mockState.user.role = "admin";
    const { container } = renderLayout("/admin/mcp-tools");
    const selected = Array.from(
      container.querySelectorAll(".ant-menu-item-selected"),
    );
    expect(selected.length).toBeGreaterThan(0);
    expect(selected[0]?.textContent).toContain("MCP 工具");
    expect(selected[0]?.textContent).not.toContain("外部接入");
  });
});
