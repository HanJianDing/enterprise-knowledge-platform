import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import GraphPage, { buildGraphOption } from "../pages/admin/GraphPage";
import type { GraphOverviewResult } from "../services/adminApi";

const mockGraphOverview = vi.fn();
const mockRefreshCommunities = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    graphOverview: (...args: unknown[]) => mockGraphOverview(...args),
    refreshGraphCommunities: (...args: unknown[]) => mockRefreshCommunities(...args),
  },
}));

// EChart 在 jsdom 下渲染需 mock echarts/core（与 EChart.test 同款）；
// 批次2-5：mock 实例补 dispatchAction 供社区高亮交互断言
// 批次F 验收修复：高亮改为 setOption 方案——mock 记录最近 option 供断言
let lastSetOption: any = null;
const mockSetOption = vi.fn((opt: unknown) => { lastSetOption = opt; });
vi.mock("echarts/core", () => ({
  init: vi.fn(() => ({
    setOption: mockSetOption,
    resize: vi.fn(),
    dispose: vi.fn(),
    dispatchAction: vi.fn(),
  })),
  use: vi.fn(),
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

const sampleData: GraphOverviewResult = {
  nodes: [
    { name: "刘强", type: "person", description: "IT 总监", community_id: 0 },
    { name: "钱进", type: "person", description: "IT 部经理", community_id: 0 },
    { name: "赵明远", type: "person", description: "财务总监", community_id: 1 },
  ],
  edges: [
    { source: "钱进", target: "刘强", relation: "上级", strength: 8 },
    { source: "赵明远", target: "王建国", relation: "上级", strength: 8 },
  ],
  communities: [
    { id: 0, members: ["刘强", "钱进"], summary: "IT 团队社区", count: 2 },
    { id: 1, members: ["赵明远"], summary: "财务社区", count: 1 },
  ],
};

describe("GraphPage 知识图谱可视化页（批次2-3）", () => {
  beforeEach(() => {
    mockGraphOverview.mockClear();
    mockRefreshCommunities.mockClear();
  });

  it("buildGraphOption：节点按社区着色、边带关系标签", () => {
    const option = buildGraphOption(sampleData) as any;
    const series = option.series[0];
    expect(series.type).toBe("graph");
    expect(series.categories).toHaveLength(2);
    // 社区 0 的两个节点同色
    const colors = series.data.map((n: any) => n.itemStyle.color);
    expect(colors[0]).toBe(colors[1]);
    expect(colors[0]).not.toBe(colors[2]);
    // 边带关系 label
    expect(series.links[0].relation).toBe("上级");
  });

  it("页面：加载后展示社区报告卡片与重算按钮", async () => {
    mockGraphOverview.mockResolvedValue(sampleData);
    render(<GraphPage />);
    expect(await screen.findByText("IT 团队社区")).toBeInTheDocument();
    expect(screen.getByText("财务社区")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重算社区/ })).toBeInTheDocument();
  });

  it("页面：点击重算社区调用刷新并提示", async () => {
    mockGraphOverview.mockResolvedValue(sampleData);
    mockRefreshCommunities.mockResolvedValue({ partition: {}, community_count: 2 });
    render(<GraphPage />);
    await screen.findByText("IT 团队社区");
    await userEvent.click(screen.getByRole("button", { name: /重算社区/ }));
    expect(mockRefreshCommunities).toHaveBeenCalled();
  });

  it("页面：空数据展示引导提示", async () => {
    mockGraphOverview.mockResolvedValue({ nodes: [], edges: [], communities: [] });
    render(<GraphPage />);
    expect(await screen.findByText(/图谱暂无数据/)).toBeInTheDocument();
  });

  it("交互（批次F 验收修复）：点击社区卡片 setOption 高亮该社区节点", async () => {
    mockGraphOverview.mockResolvedValue(sampleData);
    render(<GraphPage />);
    await screen.findByText("IT 团队社区");
    lastSetOption = null;
    await userEvent.click(screen.getByText("IT 团队社区"));
    // setOption 直改样式：社区 0 成员原色不透明，其他节点置灰半透明
    const data = lastSetOption?.series?.[0]?.data ?? [];
    const byName = Object.fromEntries(data.map((n: any) => [n.name, n]));
    expect(byName["刘强"].itemStyle.color).toBe("#5470c6");
    expect(byName["刘强"].itemStyle.opacity).toBe(1);
    expect(byName["钱进"].itemStyle.opacity).toBe(1);
    expect(byName["赵明远"].itemStyle.color).toBe("#c0c4cc");
    expect(byName["赵明远"].itemStyle.opacity).toBe(0.15);
    // 挂清单 #8 修复：高亮态下边按两端节点是否都在集合内着色
    const links = lastSetOption?.series?.[0]?.links ?? [];
    const linkByPair = Object.fromEntries(
      links.map((l: any) => [`${l.source}->${l.target}`, l])
    );
    // 钱进→刘强 两端都在社区0（高亮）→ 深色；赵明远→王建国 有一端不在 → 浅灰
    expect(linkByPair["钱进->刘强"].lineStyle.color).toBe("#8a93a6");
    expect(linkByPair["赵明远->王建国"].lineStyle.color).toBe("#e0e2e6");
    // 再点同一社区 → 取消高亮（恢复原色）
    await userEvent.click(screen.getByText("IT 团队社区"));
    const data2 = lastSetOption?.series?.[0]?.data ?? [];
    const byName2 = Object.fromEntries(data2.map((n: any) => [n.name, n]));
    expect(byName2["刘强"].itemStyle.opacity).toBeUndefined();
    expect(byName2["赵明远"].itemStyle.color).toBe("#91cc75");
  });
});
