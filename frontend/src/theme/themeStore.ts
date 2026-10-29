/** 主题模式 store：亮/暗切换 + localStorage 持久化
 *
 * - mode 变化时由 App.tsx 重注入 CSS 变量、切换 antd algorithm
 * - useThemeTokens() 是组件取当前模式调色板的统一入口（替代原来的 `import { tokens }`）
 */
import { create } from "zustand";
import { getPalette, type Palette, type ThemeMode } from "./tokens";

const STORAGE_KEY = "theme_mode";

/** 读取持久化模式（main.tsx 也用它做首屏注入，避免暗色用户闪白） */
export function getStoredMode(): ThemeMode {
  return localStorage.getItem(STORAGE_KEY) === "dark" ? "dark" : "light";
}

export const useThemeStore = create<{
  mode: ThemeMode;
  toggleMode: () => void;
}>((set) => ({
  mode: getStoredMode(),
  toggleMode: () =>
    set((s) => {
      const mode: ThemeMode = s.mode === "light" ? "dark" : "light";
      localStorage.setItem(STORAGE_KEY, mode);
      return { mode };
    }),
}));

/** 组件内取当前模式的调色板（与 antd token / CSS 变量同源，不会漂移） */
export const useThemeTokens = (): Palette => {
  const mode = useThemeStore((s) => s.mode);
  return getPalette(mode);
};
