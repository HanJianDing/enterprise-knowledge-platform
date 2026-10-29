import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import DashboardPage from "../pages/admin/DashboardPage";

const mockGetDashboard = vi.fn();
const mockGetQuickQuestions = vi.fn();
const mockSetQuickQuestions = vi.fn();
const mockGetFeedbackStats = vi.fn();
const mockGetDashboardStats = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    getDashboard: (...args: unknown[]) => mockGetDashboard(...args),
    getQuickQuestions: (...args: unknown[]) => mockGetQuickQuestions(...args),
    setQuickQuestions: (...args: unknown[]) => mockSetQuickQuestions(...args),
    getFeedbackStats: (...args: unknown[]) => mockGetFeedbackStats(...args),
    getDashboardStats: (...args: unknown[]) => mockGetDashboardStats(...args),
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

// antd 静态 message 兼容层在 jsdom 下渲染提示不报错即可，mock 掉避免异步影响
vi.mock("../utils/message", () => ({
  message: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
    loading: vi.fn(),
    destroy: vi.fn(),
  },
}));

describe("DashboardPage 快捷问题配置", () => {
  beforeEach(() => {
    // 清掉上一个用例的调用历史（not.toHaveBeenCalled 断言依赖干净的 mock）
    mockGetDashboard.mockClear();
    mockGetQuickQuestions.mockClear();
    mockSetQuickQuestions.mockClear();
    mockGetFeedbackStats.mockClear();
    mockGetDashboardStats.mockClear();
    mockGetDashboard.mockResolvedValue({
      user_count: 3,
      conversation_count: 10,
      message_count: 42,
      document_count: 5,
      documents_completed: 4,
      documents_processing: 1,
      documents_failed: 0,
    });
    mockGetQuickQuestions.mockResolvedValue({
      questions: ["问题一", "问题二"],
    });
    mockSetQuickQuestions.mockResolvedValue({});
    mockGetFeedbackStats.mockResolvedValue({
      like: 3,
      dislike: 1,
      total: 4,
      satisfaction_rate: 75,
    });
    mockGetDashboardStats.mockResolvedValue({
      trend: [
        { date: "2026-09-02", conversations: 1, messages: 2 },
        { date: "2026-09-03", conversations: 2, messages: 5 },
      ],
      feedback: { like: 3, dislike: 1, total: 4, satisfaction_rate: 75 },
      quality: { avg_latency_ms: 800, avg_tokens: 120.5 },
      categories: [{ category_id: "a", name: "尺码表", count: 3 }],
    });
  });

  it("渲染满意度统计卡（赞踩计数）", async () => {
    render(<DashboardPage />);
    expect(await screen.findByText("回答满意度")).toBeInTheDocument();
    expect(await screen.findByText("75")).toBeInTheDocument();
    expect(screen.getByText("共 4 条评价")).toBeInTheDocument();
  });

  it("渲染图表区：趋势卡与质量指标", async () => {
    render(<DashboardPage />);
    expect(await screen.findByText("对话趋势")).toBeInTheDocument();
    expect(screen.getByText("消息质量")).toBeInTheDocument();
    expect(screen.getByText("平均回答延迟")).toBeInTheDocument();
    expect(screen.getByText("知识库分类分布")).toBeInTheDocument();
    expect(mockGetDashboardStats).toHaveBeenCalledWith(7);
  });

  it("渲染统计卡与已配置的快捷问题", async () => {
    render(<DashboardPage />);
    expect(await screen.findByText("用户总数")).toBeInTheDocument();
    expect(await screen.findByDisplayValue("问题一")).toBeInTheDocument();
    expect(screen.getByDisplayValue("问题二")).toBeInTheDocument();
  });

  it("保存时去掉空条目并调用接口", async () => {
    const user = userEvent.setup();
    render(<DashboardPage />);

    // 添加一行空问题
    await screen.findByDisplayValue("问题一");
    await user.click(screen.getByRole("button", { name: /添加问题/ }));
    const inputs = await screen.findAllByPlaceholderText(/^问题 \d$/);
    expect(inputs).toHaveLength(3);

    await user.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() =>
      expect(mockSetQuickQuestions).toHaveBeenCalledWith(["问题一", "问题二"])
    );
  });

  it("删除按钮在只剩一条时禁用", async () => {
    mockGetQuickQuestions.mockResolvedValue({ questions: ["唯一一条"] });
    render(<DashboardPage />);
    expect(await screen.findByDisplayValue("唯一一条")).toBeInTheDocument();
    expect(screen.getByTitle("删除此条")).toBeDisabled();
  });

  it("全部清空后保存被拦截，不调接口", async () => {
    const user = userEvent.setup();
    render(<DashboardPage />);
    await screen.findByDisplayValue("问题一");

    // 清空所有输入
    const inputs = await screen.findAllByPlaceholderText(/^问题 \d$/);
    for (const input of inputs) {
      await user.clear(input);
    }
    await user.click(screen.getByRole("button", { name: /保\s*存/ }));
    expect(mockSetQuickQuestions).not.toHaveBeenCalled();
  });
});
