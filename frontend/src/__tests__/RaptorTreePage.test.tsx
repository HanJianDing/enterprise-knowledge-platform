import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import RaptorTreePage from "../pages/admin/RaptorTreePage";

const mockRaptorOverview = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    raptorOverview: (...args: unknown[]) => mockRaptorOverview(...args),
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

const sampleOverview = {
  documents: [
    {
      doc_id: "doc-1",
      filename: "员工手册.txt",
      clusters: [
        {
          tree_level: 1,
          cluster_id: "L1C0",
          child_count: 4,
          child_chunk_indices: [0, 1, 2, 3],
          summary_preview: "本部分规定休假管理相关的总体要求",
          built_at: "2026-10-10T10:00:00Z",
        },
        {
          tree_level: 2,
          cluster_id: "L2C0",
          child_count: 2,
          child_chunk_indices: [101000],
          summary_preview: "本部分规定人事制度体系的总体框架",
          built_at: "2026-10-10T10:00:00Z",
        },
      ],
    },
  ],
};

describe("RaptorTreePage 摘要树可视化页（批次B）", () => {
  beforeEach(() => {
    mockRaptorOverview.mockClear();
  });

  it("加载后按文档卡片展示树结构（文件名/簇数/层级/摘要文本）", async () => {
    mockRaptorOverview.mockResolvedValue(sampleOverview);
    render(<RaptorTreePage />);
    // 文档卡片：文件名 + 簇数统计
    expect(await screen.findByText("员工手册.txt")).toBeInTheDocument();
    expect(screen.getByText(/2 簇 · 2 层/)).toBeInTheDocument();
    // 层级折叠面板标签（第 1 层 1 个簇 / 第 2 层 1 个簇）
    expect(screen.getByText(/第 1 层（1 个簇）/)).toBeInTheDocument();
    expect(screen.getByText(/第 2 层（1 个簇）/)).toBeInTheDocument();
    // Collapse 默认折叠：展开第 1 层面板后断言簇 ID 与摘要文本
    fireEvent.click(screen.getByText(/第 1 层（1 个簇）/));
    expect(await screen.findByText("L1C0")).toBeInTheDocument();
    expect(
      screen.getByText("本部分规定休假管理相关的总体要求"),
    ).toBeInTheDocument();
  });

  it("空结构展示引导说明", async () => {
    mockRaptorOverview.mockResolvedValue({ documents: [] });
    render(<RaptorTreePage />);
    expect(await screen.findByText(/暂无摘要树/)).toBeInTheDocument();
  });

  it("接口失败时提示错误", async () => {
    const { message } = await import("../utils/message");
    mockRaptorOverview.mockRejectedValue({
      response: { data: { detail: "服务器开小差" } },
    });
    render(<RaptorTreePage />);
    await screen.findByText(/暂无摘要树/);
    expect(message.error).toHaveBeenCalledWith("服务器开小差");
  });
});
