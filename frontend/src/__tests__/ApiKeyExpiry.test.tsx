// 批次F 验收回归：设置过期时间白屏（antd 6 DatePicker 传 Date 崩溃）
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import ApiKeyPage from "../pages/admin/ApiKeyPage";

const mockList = vi.fn();
const mockSetExpiry = vi.fn();
const mockRevoke = vi.fn();
const mockCreate = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    apiKeyList: (...a: unknown[]) => mockList(...a),
    setApiKeyExpiry: (...a: unknown[]) => mockSetExpiry(...a),
    revokeApiKey: (...a: unknown[]) => mockRevoke(...a),
    createApiKey: (...a: unknown[]) => mockCreate(...a),
  },
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

describe("ApiKey 过期时间设置（批次F 验收修复）", () => {
  beforeEach(() => {
    mockList.mockResolvedValue([{
      id: "k1", name: "测试1", prefix: "sk-rag-b776a2baa",
      is_active: true, expires_at: null, last_used_at: null,
      created_at: "2026-10-15T19:42:27",
    }]);
    mockSetExpiry.mockResolvedValue({ message: "ok", expires_at: "2026-11-01T00:00:00" });
  });

  it("点设置过期 → 选日期 → 保存：不崩溃且 API 被调用", async () => {
    render(<ApiKeyPage />);
    await screen.findByText("测试1");
    await userEvent.click(screen.getByRole("button", { name: /设置过期/ }));
    // Modal 出现
    expect(await screen.findByText(/设置过期时间：测试1/)).toBeInTheDocument();
    // 点保存（不选时间 = 取消过期）
    await userEvent.click(screen.getByRole("button", { name: /保 存|保存/ }));
    await waitFor(() => expect(mockSetExpiry).toHaveBeenCalled());
    expect(mockSetExpiry).toHaveBeenCalledWith("k1", null);
  });
});

describe("选日期场景（回归：value 传 dayjs 不崩溃）", () => {
  it("手动输入日期 + 保存：不崩溃且 API 收到正确 ISO 串", async () => {
    mockList.mockResolvedValue([{
      id: "k1", name: "测试1", prefix: "sk-rag-b776a2baa",
      is_active: true, expires_at: null, last_used_at: null,
      created_at: "2026-10-15T19:42:27",
    }]);
    mockSetExpiry.mockClear();
    render(<ApiKeyPage />);
    await screen.findByText("测试1");
    await userEvent.click(screen.getByRole("button", { name: /设置过期/ }));
    await screen.findByText(/设置过期时间：测试1/);
    // 手动输入日期（antd DatePicker 支持键盘输入）
    const input = document.querySelector(".ant-picker-input input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "2026-11-01 12:00:00" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await userEvent.click(screen.getByRole("button", { name: /保 存|保存/ }));
    await waitFor(() => expect(mockSetExpiry).toHaveBeenCalled());
    // 质检返工 P2-1：断言实际值（此前只断言被调用，value 为 null 也绿）。
    // 时区无关断言：ISO 串含所选日期，入参非 null（取消过期路径传 null）
    expect(mockSetExpiry).toHaveBeenCalledWith(
      "k1", expect.stringMatching(/^2026-11-01T\d{2}:\d{2}:\d{2}\.\d{3}Z$/)
    );
  });
});

describe("生成成功弹窗（批次F 验收 4.7）", () => {
  it("生成后弹窗显示明文 key", async () => {
    mockList.mockResolvedValue([]);
    mockCreate.mockResolvedValue({
      id: "k2", key: "sk-rag-newkey123", name: "测试3", prefix: "sk-rag-newk",
      created_at: "2026-10-15T19:43:05", expires_at: null,
    });
    render(<ApiKeyPage />);
    await screen.findByText("生成 Key");
    await userEvent.click(screen.getByRole("button", { name: /生成 Key/ }));
    const input = screen.getByPlaceholderText(/用途备注/);
    await userEvent.type(input, "测试3");
    await userEvent.click(screen.getByRole("button", { name: /^生 成|^生成/ }));
    // 明文 key 渲染为文本（copyable 已显式绑定 text，复制不再出
    // [object Object]——真实验收发现；复制行为本身 jsdom 无法模拟）
    expect(await screen.findByText("sk-rag-newkey123")).toBeInTheDocument();
  });
});
