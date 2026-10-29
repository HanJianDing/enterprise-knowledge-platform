import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import ApprovalRulesPage from "../pages/admin/ApprovalRulesPage";

const mockApprovalRules = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    approvalRules: (...args: unknown[]) => mockApprovalRules(...args),
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

const sampleRules = [
  {
    id: "r1",
    business_type: "报销",
    min_value: 1000,
    max_value: 5000,
    unit: "元",
    approver: "部门负责人",
    action: "审批",
    source_text: "报销1000 至 5000元：部门负责人审批",
    source_doc: "审批权限与流程制度.txt",
  },
  {
    id: "r2",
    business_type: "请假",
    min_value: 7,
    max_value: null,
    unit: "天",
    approver: "人事部",
    action: "备案",
    source_text: "请假七天以上：人事部备案",
    source_doc: "审批权限与流程制度.txt",
  },
];

describe("ApprovalRulesPage 审批规则管理页（批次2-5）", () => {
  beforeEach(() => {
    mockApprovalRules.mockClear();
  });

  it("加载后展示规则表格（区间/审批人/来源文档）", async () => {
    mockApprovalRules.mockResolvedValue(sampleRules);
    render(<ApprovalRulesPage />);
    expect(await screen.findByText("部门负责人")).toBeInTheDocument();
    expect(screen.getByText("1000 至 5000元")).toBeInTheDocument();
    expect(screen.getByText("7天以上")).toBeInTheDocument();
    expect(screen.getByText("人事部")).toBeInTheDocument();
    // 两条规则同来源文档 → 该文本出现两处
    expect(screen.getAllByText("审批权限与流程制度.txt").length).toBe(2);
    // 动作标签：备案橙色 / 审批绿色
    expect(screen.getByText("备案")).toBeInTheDocument();
  });

  it("空列表展示引导说明", async () => {
    mockApprovalRules.mockResolvedValue([]);
    render(<ApprovalRulesPage />);
    expect(await screen.findByText(/解析不了的句式/)).toBeInTheDocument();
  });
});

describe("审批人搜索（挂清单 #6）", () => {
  it("搜索财务总监 → 带 approver 参数重拉；重置 → 清空输入并全量重拉", async () => {
    mockApprovalRules.mockResolvedValue(sampleRules);
    render(<ApprovalRulesPage />);
    await screen.findByText("报销");
    mockApprovalRules.mockClear();
    const input = screen.getByPlaceholderText(/搜索审批人/);
    await userEvent.type(input, "财务总监{Enter}");
    await waitFor(() =>
      expect(mockApprovalRules).toHaveBeenCalledWith("财务总监")
    );
    // 重置：清空输入 + 无参全量重拉
    await userEvent.click(screen.getByRole("button", { name: /重\s*置/ }));
    await waitFor(() =>
      expect(mockApprovalRules).toHaveBeenCalledWith(undefined)
    );
    expect((input as HTMLInputElement).value).toBe("");
  });
});
