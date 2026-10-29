/** 看板数据 → ECharts option 的纯函数（抽出来便于单测，不含 React 依赖） */
import type { EChartsOption } from "echarts";
import type { FeedbackStats } from "../../services/adminApi";

export interface TrendPoint {
  date: string;
  conversations: number;
  messages: number;
}

export interface CategoryCount {
  category_id: string | null;
  name: string;
  count: number;
}

/** 趋势折线：对话量 + 消息量双系列 */
export function buildTrendOption(trend: TrendPoint[]): EChartsOption {
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["对话数", "消息数"], top: 0 },
    grid: { left: 40, right: 16, top: 36, bottom: 24 },
    xAxis: {
      type: "category",
      data: trend.map((t) => t.date.slice(5)), // MM-DD 展示
      axisTick: { alignWithLabel: true },
    },
    yAxis: { type: "value", minInterval: 1 },
    series: [
      {
        name: "对话数",
        type: "line",
        smooth: true,
        symbolSize: 5,
        data: trend.map((t) => t.conversations),
        itemStyle: { color: "#f97316" },
        areaStyle: { opacity: 0.08, color: "#f97316" },
      },
      {
        name: "消息数",
        type: "line",
        smooth: true,
        symbolSize: 5,
        data: trend.map((t) => t.messages),
        itemStyle: { color: "#1677ff" },
        areaStyle: { opacity: 0.06, color: "#1677ff" },
      },
    ],
  };
}

/** 满意度饼图：赞/踩占比 */
export function buildFeedbackPie(feedback: FeedbackStats): EChartsOption {
  return {
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    legend: { bottom: 0 },
    series: [
      {
        type: "pie",
        radius: ["45%", "68%"],
        center: ["50%", "45%"],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 6, borderColor: "#fff", borderWidth: 2 },
        label: { show: false },
        data: [
          { value: feedback.like, name: "点赞", itemStyle: { color: "#52c41a" } },
          { value: feedback.dislike, name: "点踩", itemStyle: { color: "#ff4d4f" } },
        ],
      },
    ],
  };
}

/** 分类文档分布柱状图（横向条形，数量倒序已由后端保证） */
export function buildCategoryBar(categories: CategoryCount[]): EChartsOption {
  // 横向条形：后端已按数量倒序，这里反转让最大条显示在顶部
  const ordered = [...categories].reverse();
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 8, right: 32, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: "value", minInterval: 1 },
    yAxis: {
      type: "category",
      data: ordered.map((c) => c.name),
      axisLabel: { width: 90, overflow: "truncate" },
    },
    series: [
      {
        type: "bar",
        data: ordered.map((c) => c.count),
        barMaxWidth: 22,
        itemStyle: {
          color: "#f97316",
          borderRadius: [0, 4, 4, 0],
        },
        label: { show: true, position: "right" },
      },
    ],
  };
}
