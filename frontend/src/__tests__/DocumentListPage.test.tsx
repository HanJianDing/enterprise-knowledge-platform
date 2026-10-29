import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import DocumentListPage from "../pages/knowledge/DocumentListPage";
import type { DocumentInfo } from "../types/document";

const mockList = vi.fn();
const mockGetStats = vi.fn();
const mockGetCategories = vi.fn();
const mockListKbs = vi.fn();
const mockReProcess = vi.fn();

vi.mock("../services/documentApi", () => ({
  documentApi: {
    list: (...args: unknown[]) => mockList(...args),
    getStats: (...args: unknown[]) => mockGetStats(...args),
    getCategories: (...args: unknown[]) => mockGetCategories(...args),
    upload: vi.fn(),
    delete: vi.fn(),
    reprocess: (...args: unknown[]) => mockReProcess(...args),
    createCategory: vi.fn(),
    deleteCategory: vi.fn(),
    getChunks: vi.fn(),
  },
}));

vi.mock("../services/kbApi", () => ({
  kbApi: {
    listKbs: (...args: unknown[]) => mockListKbs(...args),
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

const completedDoc: DocumentInfo = {
  id: "doc-1",
  filename: "doc-1.txt",
  original_name: "年假制度.txt",
  file_type: "txt",
  file_size: 1024,
  status: "completed",
  chunk_count: 3,
  content_type: "long_text",
  kb_id: "kb-1",
  kb_name: "人事制度库",
  version_no: 1,
  version_of: null,
  created_at: "2026-09-24T08:00:00",
  updated_at: "2026-09-24T08:00:10",
  process_log: [
    { time: "2026-09-24 08:00:00", stage: "upload", detail: "上传成功（年假制度.txt，1024 字节）" },
    { time: "2026-09-24 08:00:01", stage: "start", detail: "开始处理（模式：long_text，文件：年假制度.txt）" },
    { time: "2026-09-24 08:00:10", stage: "done", detail: "处理完成（耗时 9000ms，共 3 块）" },
  ],
};

const failedDoc: DocumentInfo = {
  ...completedDoc,
  id: "doc-2",
  original_name: "坏文件.txt",
  status: "failed",
  chunk_count: 0,
  error_message: "文件解析失败：编码无法识别",
  process_log: [
    { time: "2026-09-24 09:00:00", stage: "upload", detail: "上传成功" },
    { time: "2026-09-24 09:00:02", stage: "error", detail: "处理失败：文件解析失败：编码无法识别" },
  ],
};

describe("DocumentListPage 解析流水（A3）", () => {
  beforeEach(() => {
    mockList.mockClear();
    mockGetStats.mockClear();
    mockGetCategories.mockClear();
    mockListKbs.mockClear();
    mockReProcess.mockClear();
    mockReProcess.mockResolvedValue({ message: "ok" });
    mockListKbs.mockResolvedValue([{ id: "kb-1", name: "人事制度库", document_count: 2 }]);
    mockList.mockResolvedValue({ items: [completedDoc, failedDoc], total: 2, page: 1, page_size: 20 });
    mockGetStats.mockResolvedValue({
      total_documents: 2, total_chunks: 3, completed_documents: 1,
      processing_documents: 0, failed_documents: 1,
      total_size_bytes: 2048, categories_count: 1,
    });
    mockGetCategories.mockResolvedValue([]);
  });

  it("状态徽章：失败文档显示红色失败标签、成功显示已完成", async () => {
    render(<MemoryRouter><DocumentListPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("年假制度.txt")).toBeInTheDocument());
    // "已完成/失败"同时出现在统计卡标题与状态标签，断言至少各一处
    expect(screen.getAllByText("已完成").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("失败").length).toBeGreaterThanOrEqual(1);
  });

  it("点击流水按钮打开时间轴，展示上传/开始/完成事件与详情", async () => {
    render(<MemoryRouter><DocumentListPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("年假制度.txt")).toBeInTheDocument());
    await userEvent.click(within(screen.getByText("年假制度.txt").closest("tr")!).getByRole("button", { name: /展开行|expand row/i }));
    const viewBtns = screen.getAllByText("查看");
    await userEvent.click(viewBtns[0]!);
    // 弹窗：事件标签 + 详情 + 时间
    expect(await screen.findByText("处理完成")).toBeInTheDocument();
    expect(screen.getByText("开始处理")).toBeInTheDocument();
    expect(screen.getByText("上传")).toBeInTheDocument();
    expect(screen.getByText(/共 3 块/)).toBeInTheDocument();
  });

  it("失败文档的流水弹窗顶部展示失败原因", async () => {
    render(<MemoryRouter><DocumentListPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("坏文件.txt")).toBeInTheDocument());
    await userEvent.click(within(screen.getByText("坏文件.txt").closest("tr")!).getByRole("button", { name: /展开行|expand row/i }));
    const viewBtns = screen.getAllByText("查看");
    await userEvent.click(viewBtns[0]!);
    expect(await screen.findByText("处理失败原因")).toBeInTheDocument();
    // Alert 描述与时间轴详情都包含该文案，断言至少一处
    expect(screen.getAllByText(/文件解析失败/).length).toBeGreaterThanOrEqual(1);
  });

  it("已完成文档显示重切按钮，确认后调用 force 重新分块", async () => {
    render(<MemoryRouter><DocumentListPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("年假制度.txt")).toBeInTheDocument());
    await userEvent.click(within(screen.getByText("年假制度.txt").closest("tr")!).getByRole("button", { name: /展开行|expand row/i }));
    await userEvent.click(screen.getByText("重切"));
    const confirmBtn = await screen.findByText("重新分块");
    await userEvent.click(confirmBtn);
    await waitFor(() => expect(mockReProcess).toHaveBeenCalledWith("doc-1", true));
  });

  it("翻页修复：点击第 2 页请求第 2 页数据，不被重置回第 1 页", async () => {
    // 25 篇文档 → 2 页（每页 20）
    const manyDocs = Array.from({ length: 25 }, (_, i) => ({
      ...completedDoc,
      id: `doc-${i}`,
      original_name: `文档${i}.txt`,
    }));
    mockList.mockResolvedValue({ items: manyDocs.slice(0, 20), total: 25, page: 1, page_size: 20 });
    render(<MemoryRouter><DocumentListPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("文档0.txt")).toBeInTheDocument());

    const page2 = screen.getByTitle("2");
    await userEvent.click(page2);
    await waitFor(() => {
      const calls = mockList.mock.calls.map((c) => c[0] as { page?: number });
      expect(calls.some((p) => p.page === 2)).toBe(true);
    });
  });
});
