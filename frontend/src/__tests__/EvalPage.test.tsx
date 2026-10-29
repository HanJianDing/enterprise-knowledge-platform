import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import EvalPage from "../pages/admin/EvalPage";

const mockEvalRetrievalTest = vi.fn();
const mockRunEval = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    evalRetrievalTest: (...args: unknown[]) => mockEvalRetrievalTest(...args),
    runEval: (...args: unknown[]) => mockRunEval(...args),
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

describe("EvalPage 检索评估页（B4）", () => {
  beforeEach(() => {
    mockEvalRetrievalTest.mockClear();
    mockRunEval.mockClear();
  });

  it("试检索：输入问题后展示命中切片", async () => {
    mockEvalRetrievalTest.mockResolvedValue({
      query: "年假怎么请",
      top_k: 6,
      reranked: false,
      docs: [
        {
          index: 1,
          filename: "考勤制度.txt",
          chunk_index: 0,
          content: "员工年假 5 天",
          content_preview: "员工年假 5 天",
        },
      ],
    });
    render(<EvalPage />);
    const input = screen.getByPlaceholderText(/输入问题/);
    await userEvent.type(input, "年假怎么请");
    await userEvent.click(screen.getByRole("button", { name: /试检索/ }));
    expect(await screen.findByText("考勤制度.txt")).toBeInTheDocument();
    expect(screen.getByText("第 1 块")).toBeInTheDocument();
    expect(screen.getByText("员工年假 5 天")).toBeInTheDocument();
  });

  it("评估跑分：点击开始评估后展示指标卡片", async () => {
    mockRunEval.mockResolvedValue({
      total_questions: 60,
      doc_count: 31,
      with_rerank: false,
      elapsed_ms: 13100,
      standard: { n: 50, hit_rate: 0.92, mrr: 0.82, avg_ms: 180, details: [] },
      fuzzy: { n: 10, hit_rate: 0.9, mrr: 0.8, avg_ms: 180, details: [] },
      total: { n: 60, hit_rate: 0.917, mrr: 0.816, avg_ms: 180 },
    });
    render(<EvalPage />);
    await userEvent.click(screen.getByText("📊 评估跑分"));
    await userEvent.click(screen.getByRole("button", { name: /开始评估/ }));
    expect(await screen.findByText("91.7%")).toBeInTheDocument();
    // antd Statistic 会把纯小数拆成多个渲染节点，用标题定位 + 容器文本断言
    const mrrTitle = screen.getByText("MRR@6");
    expect(mrrTitle.closest(".ant-statistic")?.textContent).toContain("0.816");
    const timeTitle = screen.getByText("总耗时");
    expect(timeTitle.closest(".ant-statistic")?.textContent).toContain("13.1s");
  });

  it("审批规则题：规则引擎准确率卡片 + 详情表（批次2-4）", async () => {
    mockRunEval.mockResolvedValue({
      total_questions: 8,
      doc_count: 0,
      with_rerank: false,
      eval_set: "approval",
      elapsed_ms: 120,
      approval: {
        n: 8,
        accuracy: 1.0,
        details: [
          {
            question: "报销 3000 元谁审批？",
            hit: true,
            expected: "部门负责人审批",
            got: "部门负责人审批",
          },
          {
            question: "报销 8000 元谁审批？",
            hit: true,
            expected: "财务总监审批",
            got: "财务总监审批",
          },
        ],
      },
    });
    render(<EvalPage />);
    await userEvent.click(screen.getByText("📊 评估跑分"));
    await userEvent.click(screen.getByRole("combobox"));
    await userEvent.click(await screen.findByText("审批规则题"));
    await userEvent.click(screen.getByRole("button", { name: /开始评估/ }));
    expect(await screen.findByText(/规则引擎确定性匹配/)).toBeInTheDocument();
    const accTitle = screen.getByText("规则匹配准确率");
    expect(accTitle.closest(".ant-statistic")?.textContent).toContain("100.0%");
    expect(screen.getByText("报销 3000 元谁审批？")).toBeInTheDocument();
    expect(screen.getAllByText("部门负责人审批").length).toBeGreaterThanOrEqual(1);
  });

  it("关系题（图谱）：分环节指标卡片 + 详情表（批次2-2）", async () => {
    mockRunEval.mockResolvedValue({
      total_questions: 28,
      doc_count: 0,
      with_rerank: false,
      eval_set: "relation",
      elapsed_ms: 9595,
      relation: {
        n: 28,
        graph_available: true,
        intervention_rate: 0.9286,
        entity_link_accuracy: 1.0,
        path_accuracy: 1.0,
        neighbor_accuracy: 1.0,
        no_intervention_accuracy: 1.0,
        details: [
          {
            question: "郑浩和王建国是什么关系？",
            kind: "path",
            result: {
              entities: ["郑浩", "王建国"],
              path_steps: ["郑浩的上级是钱进", "钱进的上级是刘强"],
              neighbors: [],
            },
          },
          {
            question: "钱多多和孙少少是什么关系？",
            kind: "no_intervention",
            result: null,
          },
        ],
      },
    });
    render(<EvalPage />);
    await userEvent.click(screen.getByText("📊 评估跑分"));
    // 评估集下拉选"关系题（图谱）"（antd Select：先展开再点选项）
    await userEvent.click(screen.getByRole("combobox"));
    await userEvent.click(await screen.findByText("关系题（图谱）"));
    await userEvent.click(screen.getByRole("button", { name: /开始评估/ }));
    // 图谱可用 Alert
    expect(await screen.findByText(/图谱链路可用/)).toBeInTheDocument();
    // 分环节指标（antd Statistic 拆分渲染，用标题定位 + 容器文本断言）
    const intvTitle = screen.getByText("介入率");
    expect(intvTitle.closest(".ant-statistic")?.textContent).toContain("92.9%");
    const linkTitle = screen.getByText("实体链接准确率");
    expect(linkTitle.closest(".ant-statistic")?.textContent).toContain("100.0%");
    // 详情表：链接实体 tag / 路径陈述 / 未介入灰标
    expect(screen.getByText("郑浩")).toBeInTheDocument();
    expect(screen.getByText("王建国")).toBeInTheDocument();
    expect(screen.getByText("郑浩的上级是钱进")).toBeInTheDocument();
    expect(screen.getByText("回退向量")).toBeInTheDocument();
  });
});
