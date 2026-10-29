import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import SourceDetailDrawer from "../components/chat/SourceDetailDrawer";
import type { Citation } from "../types/chat";

const mockGetChunks = vi.fn();

vi.mock("../services/documentApi", () => ({
  documentApi: {
    getChunks: (...args: unknown[]) => mockGetChunks(...args),
  },
}));

const citation: Citation = {
  index: 1,
  doc_id: "doc-1",
  title: "年假制度.pdf",
  text: "引用片段",
  category_path: "人事制度 > 假期",
  chunk_idx: 2,
};

describe("SourceDetailDrawer 引用详情抽屉", () => {
  beforeEach(() => {
    mockGetChunks.mockClear();
    mockGetChunks.mockResolvedValue({
      document_id: "doc-1",
      total: 5,
      chunks: [
        { chunk_id: "c1", content: "上文切片内容", chunk_index: 1, chunk_hash: "h1", filename: "年假制度.pdf", category_id: "" },
        { chunk_id: "c2", content: "引用切片内容", chunk_index: 2, chunk_hash: "h2", filename: "年假制度.pdf", category_id: "" },
        { chunk_id: "c3", content: "下文切片内容", chunk_index: 3, chunk_hash: "h3", filename: "年假制度.pdf", category_id: "" },
      ],
    });
  });

  it("打开后请求附近切片，高亮引用块并展示位置与上下文", async () => {
    render(<SourceDetailDrawer citation={citation} onClose={() => {}} />);
    await waitFor(() => expect(mockGetChunks).toHaveBeenCalledWith("doc-1", 2, 1));
    expect(await screen.findByText("◎ 引用片段")).toBeInTheDocument();
    expect(screen.getByText("引用切片内容")).toBeInTheDocument();
    expect(screen.getByText("上文片段")).toBeInTheDocument();
    expect(screen.getByText("下文片段")).toBeInTheDocument();
    expect(screen.getByText("上文切片内容")).toBeInTheDocument();
    expect(screen.getByText(/第 3 \/ 5 块/)).toBeInTheDocument();
    expect(screen.getByText("年假制度.pdf")).toBeInTheDocument();
  });

  it("历史旧消息（无 chunk_idx）：降级展示引用自带片段，不发请求", () => {
    const old: Citation = { index: 1, doc_id: "doc-1", title: "旧文档", text: "旧片段" };
    render(<SourceDetailDrawer citation={old} onClose={() => {}} />);
    expect(screen.getByText("旧片段")).toBeInTheDocument();
    expect(screen.getByText(/历史消息/)).toBeInTheDocument();
    expect(mockGetChunks).not.toHaveBeenCalled();
  });

  it("加载失败显示错误文案", async () => {
    mockGetChunks.mockRejectedValue(new Error("fail"));
    render(<SourceDetailDrawer citation={citation} onClose={() => {}} />);
    expect(await screen.findByText(/加载失败/)).toBeInTheDocument();
  });

  it("点击关闭按钮触发 onClose", async () => {
    const onClose = vi.fn();
    render(<SourceDetailDrawer citation={citation} onClose={onClose} />);
    const closeBtn = await screen.findByLabelText("Close");
    await userEvent.click(closeBtn);
    expect(onClose).toHaveBeenCalled();
  });
});
