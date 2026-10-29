import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import WorkbenchPage from "../pages/WorkbenchPage";

const mockGetDashboard = vi.fn();
const mockGetDashboardStats = vi.fn();
const mockGetQuickQuestions = vi.fn();
const mockGetStats = vi.fn();
const mockGetConversations = vi.fn();
vi.mock("../services/chatApi", () => ({
  chatApi: { getConversations: (...args: unknown[]) => mockGetConversations(...args) },
}));

vi.mock("../services/adminApi", () => ({
  adminApi: {
    getDashboard: (...args: unknown[]) => mockGetDashboard(...args),
    getDashboardStats: (...args: unknown[]) => mockGetDashboardStats(...args),
    getQuickQuestions: (...args: unknown[]) => mockGetQuickQuestions(...args),
  },
}));

vi.mock("../services/documentApi", () => ({
  documentApi: {
    getStats: (...args: unknown[]) => mockGetStats(...args),
  },
}));

// jsdom 无 canvas，EChart 依赖的 echarts/core 必须 mock
vi.mock("echarts/core", () => ({
  init: vi.fn(() => ({
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
  })),
  use: vi.fn(),
}));

// 用可变状态模拟 authStore：isAdmin / isAuditViewer / 用户信息按用例切换
const mockAuthState: {
  user: { username: string; role: string } | null;
  isAdmin: boolean;
  isAuditViewer: boolean;
} = {
  user: { username: "张三", role: "admin" },
  isAdmin: true,
  isAuditViewer: true,
};
vi.mock("../stores/authStore", () => ({
  useAuthStore: (selector?: (s: unknown) => unknown) => {
    if (selector) return selector(mockAuthState);
    return mockAuthState;
  },
}));

// 路由跳转用 mock，断言点击后调用目标路径
const mockNavigate = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => mockNavigate };
});

describe("WorkbenchPage 工作台首页", () => {
  beforeEach(() => {
    mockGetDashboard.mockClear();
    mockGetDashboardStats.mockClear();
    mockGetQuickQuestions.mockClear();
    mockGetStats.mockClear();
    mockNavigate.mockClear();
    mockGetConversations.mockResolvedValue({ items: [] });
    mockAuthState.user = { username: "张三", role: "admin" };
    mockAuthState.isAdmin = true;
    mockAuthState.isAuditViewer = true;
    mockGetDashboard.mockResolvedValue({
      user_count: 3,
      conversation_count: 10,
      message_count: 42,
      document_count: 5,
      documents_completed: 4,
      documents_processing: 1,
      documents_failed: 0,
    });
    mockGetDashboardStats.mockResolvedValue({
      trend: [],
      feedback: { like: 0, dislike: 0, total: 0, satisfaction_rate: null },
      quality: { avg_latency_ms: null, avg_tokens: null },
      categories: [],
    });
    mockGetQuickQuestions.mockResolvedValue({ questions: ["年假怎么请？", "报销流程是什么？"] });
    mockGetStats.mockResolvedValue({
      total_documents: 8,
      total_chunks: 120,
      completed_documents: 7,
      processing_documents: 0,
      failed_documents: 0,
      total_size_bytes: 1024,
      categories_count: 3,
    });
  });

  it("管理员：问候 + 全局统计 + 管理入口卡片", async () => {
    render(<WorkbenchPage />);
    expect(await screen.findByText(/张三/)).toBeInTheDocument();
    // 全局统计（admin 走 adminApi.getDashboard）
    await waitFor(() => expect(screen.getByText("5")).toBeInTheDocument());
    expect(screen.getByText("知识文档")).toBeInTheDocument();
    // 管理入口
    expect(screen.getByText("用户管理")).toBeInTheDocument();
    expect(screen.getByText("审计日志")).toBeInTheDocument();
    expect(mockGetStats).not.toHaveBeenCalled();
  });

  it("普通员工：知识库统计 + 无管理入口", async () => {
    mockAuthState.isAdmin = false;
    mockAuthState.isAuditViewer = false;
    render(<WorkbenchPage />);
    await waitFor(() => expect(screen.getByText("8")).toBeInTheDocument());
    expect(screen.getByText("可见文档")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.queryByText("用户管理")).not.toBeInTheDocument();
    expect(mockGetDashboard).not.toHaveBeenCalled();
  });

  it("输入问题后进入聊天草稿，不自动发送", async () => {
    render(<WorkbenchPage />);
    await userEvent.type(screen.getByRole("textbox", { name: "向知识助手提问" }), "报销材料");
    await userEvent.click(screen.getByRole("button", { name: /开始提问/ }));
    expect(mockNavigate).toHaveBeenCalledWith("/chat", { state: { draft: "报销材料" } });
  });

  it("推荐问题渲染并可点击进问答页", async () => {
    render(<WorkbenchPage />);
    const q = await screen.findByText("年假怎么请？");
    await userEvent.click(q);
    expect(mockNavigate).toHaveBeenCalledWith("/chat", { state: { draft: "年假怎么请？" } });
  });
  it("概览失败时提示错误，不显示虚假的零统计", async () => {
    mockGetDashboard.mockRejectedValueOnce(new Error("offline"));
    render(<WorkbenchPage />);
    expect(await screen.findByText(/概览加载失败/)).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
