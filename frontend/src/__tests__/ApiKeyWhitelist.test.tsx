// 批次F 挂清单 #12：API key 库白名单——创建时可选库范围、列表回显
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import ApiKeyPage from "../pages/admin/ApiKeyPage";

const mockList = vi.fn();
const mockCreate = vi.fn();
const mockRevoke = vi.fn();
const mockSetExpiry = vi.fn();
const mockListKbs = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    apiKeyList: (...a: unknown[]) => mockList(...a),
    createApiKey: (...a: unknown[]) => mockCreate(...a),
    revokeApiKey: (...a: unknown[]) => mockRevoke(...a),
    setApiKeyExpiry: (...a: unknown[]) => mockSetExpiry(...a),
  },
}));

vi.mock("../services/kbApi", () => ({
  kbApi: { listKbs: (...a: unknown[]) => mockListKbs(...a) },
}));

vi.mock("../utils/message", () => ({
  message: {
    success: vi.fn(), error: vi.fn(), info: vi.fn(),
    warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(),
  },
}));

vi.mock("../components/common/PageHeader", () => ({
  default: ({ title }: { title: string }) => <div>{title}</div>,
}));

describe("ApiKey 库白名单（批次F 挂清单 #12）", () => {
  beforeEach(() => {
    mockList.mockClear();
    mockCreate.mockClear();
    mockRevoke.mockClear();
    mockSetExpiry.mockClear();
    mockListKbs.mockClear();
    mockList.mockResolvedValue([
      {
        id: "k1", name: "全库 key", prefix: "sk-rag-b776a2baa",
        is_active: true, kb_ids: null, expires_at: null,
        last_used_at: null, created_at: "2026-10-15T19:42:27",
      },
      {
        id: "k2", name: "财务库 key", prefix: "sk-rag-c887b3cbb",
        is_active: true, kb_ids: ["kb-1"], expires_at: null,
        last_used_at: null, created_at: "2026-10-15T19:42:28",
      },
    ]);
    mockListKbs.mockResolvedValue([
      { id: "kb-1", name: "财务制度库", document_count: 5 },
      { id: "kb-2", name: "人事制度库", document_count: 3 },
    ]);
    mockCreate.mockResolvedValue({
      id: "k3", key: "sk-rag-new", name: "新 key", prefix: "sk-rag-new",
      kb_ids: ["kb-1"], created_at: "2026-10-16T08:00:00", expires_at: null,
    });
  });

  it("列表回显库范围：null=全部可见库、白名单=库名", async () => {
    render(<ApiKeyPage />);
    expect(await screen.findByText("全库 key")).toBeInTheDocument();
    expect(screen.getByText("全部可见库")).toBeInTheDocument();
    expect(screen.getByText("财务库 key")).toBeInTheDocument();
    expect(screen.getByText("财务制度库")).toBeInTheDocument();
  });

  it("创建时选择库 → createApiKey 收到 kb_ids", async () => {
    render(<ApiKeyPage />);
    await userEvent.click(await screen.findByRole("button", { name: /生成\s*Key/ }));
    await userEvent.type(screen.getByPlaceholderText(/用途备注/), "财务系统接入");
    // 打开库多选下拉并选择"人事制度库"——该库名只存在于下拉选项
    // （列表"库范围"列仅回显财务库 Tag，无文本歧义）
    await userEvent.click(screen.getByRole("combobox"));
    await userEvent.click(await screen.findByText("人事制度库"));
    await userEvent.click(screen.getByRole("button", { name: /^生\s*成$/ }));
    await waitFor(() => expect(mockCreate).toHaveBeenCalledWith(
      "财务系统接入", null, ["kb-2"],
    ));
  });

  it("创建时不选库 → kb_ids 传 null（穿透账号权限）", async () => {
    render(<ApiKeyPage />);
    await userEvent.click(await screen.findByRole("button", { name: /生成\s*Key/ }));
    await userEvent.type(screen.getByPlaceholderText(/用途备注/), "通用 key");
    await userEvent.click(screen.getByRole("button", { name: /^生\s*成$/ }));
    await waitFor(() => expect(mockCreate).toHaveBeenCalledWith(
      "通用 key", null, null,
    ));
  });
});
