import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import FeedbackListPage from "../pages/admin/FeedbackListPage";

const mockGetFeedbackList = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    getFeedbackList: (...args: unknown[]) => mockGetFeedbackList(...args),
  },
}));

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

const sampleItems = [
  {
    id: "f1",
    rating: "dislike" as const,
    comment: "尺码推荐错误",
    created_at: "2026-09-03T10:00:00",
    username: "张三",
    conversation_title: "尺码咨询",
    message_content: "建议选 L 码。",
  },
  {
    id: "f2",
    rating: "like" as const,
    comment: null,
    created_at: "2026-09-03T11:00:00",
    username: "李四",
    conversation_title: "退换货咨询",
    message_content: "支持七天无理由退货。",
  },
  {
    id: "f3",
    rating: "dislike" as const,
    comment: null,
    created_at: "2026-09-03T12:00:00",
    username: "王五",
    conversation_title: "物流咨询",
    message_content: "3 天内发货。",
  },
];

describe("FeedbackListPage 满意度反馈明细", () => {
  beforeEach(() => {
    mockGetFeedbackList.mockClear();
    mockGetFeedbackList.mockResolvedValue({
      items: sampleItems,
      total: 3,
      page: 1,
      page_size: 20,
    });
  });

  it("渲染明细行：用户/评价/意见（点赞行显示—，点踩无意见才显示未填写）", async () => {
    render(<FeedbackListPage />);
    expect(await screen.findByText("张三")).toBeInTheDocument();
    expect(screen.getByText("尺码推荐错误")).toBeInTheDocument();
    expect(screen.getByText("李四")).toBeInTheDocument();
    expect(screen.getByText("王五")).toBeInTheDocument();
    // 点赞行意见列显示"—"，不出现误导性的"（未填写）"
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getAllByText("（未填写）")).toHaveLength(1);
    expect(mockGetFeedbackList).toHaveBeenCalledWith({
      page: 1,
      page_size: 20,
      rating: undefined,
      has_comment: undefined,
    });
  });

  it("切换评价过滤后重新请求并重置页码", async () => {
    const user = userEvent.setup();
    render(<FeedbackListPage />);
    await screen.findByText("张三");

    // antd Select 的 placeholder span 有 pointer-events:none，点真正的 combobox 输入框打开下拉
    // （页面共两个 Select：评价过滤在前）
    await user.click(screen.getAllByRole("combobox")[0]!);
    // 选项渲染在 portal 的 .ant-select-dropdown 容器内（表格里也有"👎 点踩"Tag，必须限定范围）
    const dropdown = document.querySelector(".ant-select-dropdown");
    expect(dropdown).not.toBeNull();
    const option = await within(dropdown as HTMLElement).findByText("👎 点踩");
    await user.click(option);

    await waitFor(() =>
      expect(mockGetFeedbackList).toHaveBeenLastCalledWith({
        page: 1,
        page_size: 20,
        rating: "dislike",
        has_comment: undefined,
      })
    );
  });
});
