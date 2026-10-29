import React, { useEffect, useRef } from "react";
// 按需引入（上线优化）：整包 echarts 约 1.44MB，按需注册后预计减 60%+
import * as echarts from "echarts/core";
// 批次2-3：补 GraphChart——图谱可视化页用 graph 类型；此前未注册时
// force 布局图渲染空白且无报错（质检拦截的阻塞缺陷）
import { LineChart, PieChart, BarChart, GraphChart } from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsType } from "echarts/core";

echarts.use([
  LineChart,
  PieChart,
  BarChart,
  GraphChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer,
]);

interface EChartProps {
  option: Parameters<EChartsType["setOption"]>[0];
  height?: number;
  style?: React.CSSProperties;
}

/** 通用 ECharts 薄封装：初始化 + option 更新 + 容器尺寸自适应 + 卸载销毁。
 * 挂清单 #7 修复：onInit 回调已移除——图谱页改 setOption 高亮方案后
 * 唯一消费者消失，孤儿接口成死代码。 */
const EChart: React.FC<EChartProps> = ({ option, height = 300, style }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current);
    chartRef.current = chart;
    // 容器尺寸变化（窗口缩放/侧栏展开）时自动 resize
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(containerRef.current);
    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    // notMerge=true：每次全新渲染，避免切换 7/30 天时残留旧系列
    chartRef.current?.setOption(option, true);
  }, [option]);

  return <div ref={containerRef} style={{ width: "100%", height, ...style }} />;
};

export default EChart;
