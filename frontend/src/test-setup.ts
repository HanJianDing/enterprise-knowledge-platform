import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// waitFor 全局超时 1s→5s（批次2-2 排查结论：Windows 高负载下并行全量跑时
// jsdom 渲染偶发超 1s，waitFor 默认超时不足导致随机失败；串行/低并发均稳定。
// 加长超时不改变测试语义——断言本身不变，只是给慢机器更多等待时间）
configure({ asyncUtilTimeout: 5000 });

// Mock ResizeObserver (antd Input.TextArea 依赖)
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
window.ResizeObserver = ResizeObserverMock as any;

// Mock matchMedia (antd 响应式布局依赖)
window.matchMedia = window.matchMedia || ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: () => {},
  removeListener: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => false,
}));

// Mock scrollTo (jsdom 不实现)
window.scrollTo = () => {};
Element.prototype.scrollTo = () => {};
Element.prototype.scrollIntoView = () => {};
