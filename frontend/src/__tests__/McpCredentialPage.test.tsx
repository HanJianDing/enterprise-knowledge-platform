import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import McpCredentialPage from "../pages/admin/McpCredentialPage";

const mockList = vi.fn();
const mockCreate = vi.fn();
const mockRevoke = vi.fn();
const mockListKbs = vi.fn();

vi.mock("../services/adminApi", () => ({
  mcpCredentialApi: {
    list: (...args: unknown[]) => mockList(...args),
    create: (...args: unknown[]) => mockCreate(...args),
    revoke: (...args: unknown[]) => mockRevoke(...args),
  },
}));

vi.mock("../services/kbApi", () => ({
  kbApi: {
    listKbs: (...args: unknown[]) => mockListKbs(...args),
  },
}));

vi.mock("../utils/message", () => ({
  message: {
    success: vi.fn(), error: vi.fn(), info: vi.fn(),
    warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(),
  },
}));

describe("McpCredentialPage 外部接入管理（批次D）", () => {
  beforeEach(() => {
    mockList.mockClear();
    mockCreate.mockClear();
    mockRevoke.mockClear();
    mockListKbs.mockClear();
    mockList.mockResolvedValue({
      items: [{
        id: "cred-1", name: "OA系统", owner_username: "admin",
        kb_ids: null, revoked: false,
        created_at: "2026-09-27T08:00:00", revoked_at: null,
      }],
    });
    mockListKbs.mockResolvedValue([{ id: "kb-1", name: "人事制度库", document_count: 10 }]);
    mockCreate.mockResolvedValue({
      id: "cred-2", name: "Claude Desktop", token: "mcpk_abc123",
      kb_ids: null, message: "凭证原文仅本次展示，请立即复制保存",
    });
    mockRevoke.mockResolvedValue({ message: "已撤销接入凭证「OA系统」" });
  });

  it("列表展示凭证与状态", async () => {
    render(<McpCredentialPage />);
    expect(await screen.findByText("OA系统")).toBeInTheDocument();
    expect(screen.getByText("有效")).toBeInTheDocument();
    expect(screen.getByText("全部可见库")).toBeInTheDocument();
  });

  it("新建凭证后一次性展示原文", async () => {
    render(<McpCredentialPage />);
    await userEvent.click(await screen.findByRole("button", { name: /新建接入凭证/ }));
    const input = screen.getByPlaceholderText(/OA 系统/);
    await userEvent.type(input, "Claude Desktop");
    await userEvent.click(screen.getByRole("button", { name: /创\s*建/ }));
    expect(await screen.findByText("mcpk_abc123")).toBeInTheDocument();
    expect(screen.getByText(/只展示这一次/)).toBeInTheDocument();
  });

  it("撤销凭证走确认流程", async () => {
    render(<McpCredentialPage />);
    const revokeBtn = await screen.findByRole("button", { name: /撤\s*销/ });
    await userEvent.click(revokeBtn);
    // Popconfirm 确认按钮（okText=撤销）出现后点击
    const confirmBtn = await screen.findByRole("button", { name: /^撤\s*销$/ });
    await userEvent.click(confirmBtn);
    await waitFor(() => expect(mockRevoke).toHaveBeenCalledWith("cred-1"));
  });
});
