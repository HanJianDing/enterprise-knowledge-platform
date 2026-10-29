import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import RetrievalTuningPage from "../pages/admin/RetrievalTuningPage";

const mockGetTerms = vi.fn();
const mockSaveTerms = vi.fn();
const mockGetSuggestions = vi.fn();
const mockAdopt = vi.fn();
const mockIgnore = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    getRetrievalTerms: (...args: unknown[]) => mockGetTerms(...args),
    saveRetrievalTerms: (...args: unknown[]) => mockSaveTerms(...args),
    getRetrievalSuggestions: (...args: unknown[]) => mockGetSuggestions(...args),
    adoptRetrievalTerm: (...args: unknown[]) => mockAdopt(...args),
    ignoreRetrievalTerm: (...args: unknown[]) => mockIgnore(...args),
  },
}));

vi.mock("../utils/message", () => ({
  message: {
    success: vi.fn(), error: vi.fn(), info: vi.fn(),
    warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(),
  },
}));

describe("RetrievalTuningPage 检索调优（批次1·1a）", () => {
  beforeEach(() => {
    mockGetTerms.mockClear();
    mockSaveTerms.mockClear();
    mockGetSuggestions.mockClear();
    mockAdopt.mockClear();
    mockIgnore.mockClear();
    mockGetSuggestions.mockResolvedValue({ suggestions: [] });
    mockAdopt.mockResolvedValue({ message: "已采纳并生效" });
    mockGetTerms.mockResolvedValue({
      synonyms: { "礼品": ["礼金"] },
      weights: { "报销": 2.0 },
    });
    mockSaveTerms.mockResolvedValue({ message: "已保存" });
  });

  it("加载并展示已有同义词与词权重", async () => {
    render(<RetrievalTuningPage />);
    expect(await screen.findByDisplayValue("礼品")).toBeInTheDocument();
    expect(screen.getByText("礼金")).toBeInTheDocument(); // Select tag 展示
    expect(screen.getByDisplayValue("报销")).toBeInTheDocument();
  });

  it("保存时提交同义词与权重配置", async () => {
    const user = userEvent.setup();
    render(<RetrievalTuningPage />);
    await screen.findByDisplayValue("礼品");
    await user.click(screen.getByRole("button", { name: /保存配置/ }));
    expect(mockSaveTerms).toHaveBeenCalledWith({
      synonyms: { "礼品": ["礼金"] },
      weights: { "报销": 2.0 },
    });
  });
});
