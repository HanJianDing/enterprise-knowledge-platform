import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import DocumentVersionPage from "../pages/admin/DocumentVersionPage";

const mockGetVersions = vi.fn();
const mockGetVersionContent = vi.fn();
const mockRestoreVersion = vi.fn();

vi.mock("../services/documentApi", () => ({
  documentApi: {
    getVersions: (...args: unknown[]) => mockGetVersions(...args),
    getVersionContent: (...args: unknown[]) => mockGetVersionContent(...args),
    restoreVersion: (...args: unknown[]) => mockRestoreVersion(...args),
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

const CHAIN = [
  { id: "v1", version_no: 1, status: "superseded", created_at: "2026-01-01T00:00:00", chunk_count: 3 },
  { id: "v2", version_no: 2, status: "completed", created_at: "2026-02-01T00:00:00", chunk_count: 5 },
];

const CONTENT_V1 = {
  doc_id: "v1", version_no: 1, filename: "考勤管理制度.txt",
  content_type: "long_text", content: "第一条 上班时间 9 点\n第二条 下班时间 18 点",
};
const CONTENT_V2 = {
  doc_id: "v2", version_no: 2, filename: "考勤管理制度.txt",
  content_type: "long_text", content: "第一条 上班时间 9 点\n第二条 下班时间 17 点 30 分",
};

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={["/admin/documents/doc1/versions"]}>
      <Routes>
        <Route
          path="/admin/documents/:docId/versions"
          element={<DocumentVersionPage />}
        />
      </Routes>
    </MemoryRouter>,
  );

describe("DocumentVersionPage（批次F 前置·diff 视图）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetVersions.mockResolvedValue(CHAIN);
    mockGetVersionContent.mockImplementation((id: string) =>
      Promise.resolve(id === "v1" ? CONTENT_V1 : CONTENT_V2),
    );
  });

  it("加载版本链并展示版本号与状态标签", async () => {
    renderPage();
    await waitFor(() => {
      expect(mockGetVersions).toHaveBeenCalledWith("doc1");
    });
    // antd List.Meta 会把"第 1 版"拆成多个文本节点，用正则跨节点匹配
    expect(await screen.findByText(/第\s*1\s*版/)).toBeTruthy();
    expect(await screen.findByText("已被替换")).toBeTruthy();
    expect(await screen.findByText("当前版本")).toBeTruthy();
  });

  it("默认选中最新两版并渲染行级 diff（红删绿增）", async () => {
    renderPage();
    // 旧版独有行（18 点下班）标红删除，新版行（17 点 30 分）标绿新增；
    // prefix+行文本在同一元素内整体渲染，正则锚定行首
    const removed = await screen.findByText(/^- 第二条 下班时间 18 点$/m);
    expect(removed).toBeTruthy();
    const added = await screen.findByText(/^\+ 第二条 下班时间 17 点 30 分$/m);
    expect(added).toBeTruthy();
    // 未变行保留（TestingLibrary 默认折叠连续空白，双空格归一为单空格）
    const kept = await screen.findByText(/第一条 上班时间 9 点/);
    expect(kept).toBeTruthy();
  });

  it("单版本链提示需要重传产生历史版本", async () => {
    mockGetVersions.mockResolvedValue([CHAIN[1]]);
    renderPage();
    expect(
      await screen.findByText("该文档只有一个版本（重传同文档后才会产生历史版本）"),
    ).toBeTruthy();
  });

  it("图片文档（无文本内容）提示不支持对比", async () => {
    mockGetVersions.mockResolvedValue(CHAIN);
    mockGetVersionContent.mockResolvedValue({
      ...CONTENT_V1,
      content_type: "picture",
      content: "",
    });
    renderPage();
    expect(
      await screen.findByText("所选版本为图片文档，无纯文本内容，不支持对比"),
    ).toBeTruthy();
  });

  it("旧版回溯按钮存在且调用 restore API", async () => {
    mockRestoreVersion.mockResolvedValue({ restored_id: "v1", version_no: 1 });
    renderPage();
    const btn = await screen.findByText("回溯");
    btn.click();
    // Popconfirm 二次确认：点确认按钮才真正调 restore
    const confirm = await screen.findByRole("button", { name: /确定|OK/i });
    confirm.click();
    await waitFor(() => {
      expect(mockRestoreVersion).toHaveBeenCalledWith("v1");
    });
  });
});
