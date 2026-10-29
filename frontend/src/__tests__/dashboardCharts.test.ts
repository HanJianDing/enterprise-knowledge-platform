import { describe, it, expect } from "vitest";
import {
  buildTrendOption,
  buildFeedbackPie,
  buildCategoryBar,
} from "../pages/admin/dashboardCharts";

describe("dashboardCharts 数据转图表配置", () => {
  it("趋势折线：双系列数据与 MM-DD 日期标签", () => {
    const option = buildTrendOption([
      { date: "2026-09-01", conversations: 1, messages: 2 },
      { date: "2026-09-02", conversations: 0, messages: 3 },
    ]);
    const xAxis = (option.xAxis as { data: string[] }).data;
    expect(xAxis).toEqual(["09-01", "09-02"]);

    const series = option.series as { name: string; data: number[] }[];
    expect(series[0]!.name).toBe("对话数");
    expect(series[0]!.data).toEqual([1, 0]);
    expect(series[1]!.name).toBe("消息数");
    expect(series[1]!.data).toEqual([2, 3]);
  });

  it("满意度饼图：赞踩数值进 series data", () => {
    const option = buildFeedbackPie({
      like: 7,
      dislike: 3,
      total: 10,
      satisfaction_rate: 70,
    });
    const pie = (option.series as { data: { name: string; value: number }[] }[])[0]!;
    expect(pie.data).toEqual([
      { value: 7, name: "点赞", itemStyle: { color: "#52c41a" } },
      { value: 3, name: "点踩", itemStyle: { color: "#ff4d4f" } },
    ]);
  });

  it("分类柱状图：反转后最大条在顶部（后端倒序入参）", () => {
    const option = buildCategoryBar([
      { category_id: "a", name: "分类A", count: 5 },
      { category_id: "b", name: "分类B", count: 1 },
    ]);
    const yAxis = (option.yAxis as { data: string[] }).data;
    expect(yAxis).toEqual(["分类B", "分类A"]);
    const series = option.series as { data: number[] }[];
    expect(series[0]!.data).toEqual([1, 5]);
  });
});
