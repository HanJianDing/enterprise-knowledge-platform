import { render } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import EChart from "../components/charts/EChart";

const mockSetOption = vi.fn();
const mockResize = vi.fn();
const mockDispose = vi.fn();

// 组件按需引入，运行时入口是 echarts/core（charts/components 注册在 use() 里）
vi.mock("echarts/core", () => ({
  init: vi.fn(() => ({
    setOption: mockSetOption,
    resize: mockResize,
    dispose: mockDispose,
  })),
  use: vi.fn(),
}));

describe("EChart 通用封装", () => {
  beforeEach(() => {
    mockSetOption.mockClear();
    mockResize.mockClear();
    mockDispose.mockClear();
  });

  it("初始化后 setOption 传入配置", () => {
    const option = { xAxis: { type: "category" as const }, series: [] };
    render(<EChart option={option} height={200} />);
    expect(mockSetOption).toHaveBeenCalledWith(option, true);
  });

  it("卸载时销毁实例", () => {
    const { unmount } = render(<EChart option={{}} />);
    unmount();
    expect(mockDispose).toHaveBeenCalled();
  });
});
