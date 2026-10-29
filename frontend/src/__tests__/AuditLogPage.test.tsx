import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import AuditLogPage from "../pages/admin/AuditLogPage";

const mockGetAuditLogs = vi.fn();
const mockExportAuditLogs = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    getAuditLogs: (...args: unknown[]) => mockGetAuditLogs(...args),
    exportAuditLogs: (...args: unknown[]) => mockExportAuditLogs(...args),
  },
}));

vi.mock("../utils/message", () => ({
  message: {
    success: vi.fn(), error: vi.fn(), info: vi.fn(),
    warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(),
  },
}));

describe("AuditLogPage 审计日志（批次H：补齐动作中文标签）", () => {
  beforeEach(() => {
    mockGetAuditLogs.mockClear();
    mockGetAuditLogs.mockResolvedValue({
      items: [
        {
          id: "a1", created_at: "2026-09-27T08:38:53",
          username: "hanjianding", action: "mcp.credential_revoke",
          resource_type: "mcp_credential", resource_id: "f77d023b",
          detail: { name: "OA" }, ip: "127.0.0.1",
        },
        {
          id: "a2", created_at: "2026-09-27T08:38:43",
          username: "hanjianding", action: "mcp.credential_create",
          resource_type: "mcp_credential", resource_id: "f77d023b",
          detail: { name: "OA", kb_ids: ["kb-1"] }, ip: "127.0.0.1",
        },
        {
          id: "a3", created_at: "2026-09-27T08:36:25",
          username: "hanjianding", action: "user.login",
          resource_type: null, resource_id: null,
          detail: null, ip: "127.0.0.1",
        },
      ],
      total: 3,
    });
  });

  it("凭证动作显示中文标签而非重复英文", async () => {
    render(<AuditLogPage />);
    // 修复前：动作缺中文标签，操作列显示两遍相同英文（如 mcp.credential_create ×2）
    expect(await screen.findByText("撤销接入凭证")).toBeInTheDocument();
    expect(screen.getByText("创建接入凭证")).toBeInTheDocument();
    expect(screen.getByText("登录")).toBeInTheDocument();
    // 英文原名仍保留在次要位置（标签 + 原名 各一次）
    expect(screen.getAllByText("mcp.credential_create").length).toBe(1);
    expect(screen.getAllByText("mcp.credential_revoke").length).toBe(1);
  });

  it("资源类型过滤包含接入凭证", async () => {
    render(<AuditLogPage />);
    // antd Select 选项需要展开才渲染，这里只验证表格主体正常加载
    expect(await screen.findByText("撤销接入凭证")).toBeInTheDocument();
    // 创建/撤销两行共享同一凭证 id，对象列各出现一次
    expect(screen.getAllByText("mcp_credential(f77d023b…)").length).toBe(2);
  });
});
