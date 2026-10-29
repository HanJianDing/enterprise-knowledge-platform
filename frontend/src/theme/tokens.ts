/** 全局设计 token —— 全站唯一的设计变量来源
 *
 * 企业品牌化改造：品牌色从电商橙改为企业蓝，并支持亮/暗双主题。
 *
 * 规则：
 * - 所有页面/组件的品牌色、圆角、阴影一律从这里取，禁止再写硬编码十六进制色
 * - palettes.light / palettes.dark 的键一一对应；themeStore 切换模式时
 *   getCssVariables(mode) 重新注入 CSS 变量、getAntdThemeToken(palette) 重算 antd token
 * - 侧边栏（siderFrom/siderTo）两种模式下都保持深色渐变（深色导航是通用做法）
 */
export type ThemeMode = "light" | "dark";

export interface Palette {
  color: {
    /** 品牌主色 */
    primary: string;
    primaryHover: string;
    primaryActive: string;
    primaryBg: string;
    primaryBorder: string;
    primaryText: string;
    /** 历史人工客服消息标识色（阶段2剥离后仅用于渲染历史会话） */
    brand: string;
    brandBg: string;
    /** 侧边导航深色渐变（两种模式共用） */
    siderFrom: string;
    siderTo: string;
    /** 中性色 */
    textPrimary: string;
    textSecondary: string;
    textTertiary: string;
    textFaint: string;
    /** 表面色 */
    white: string;
    contentBg: string;
    layoutBg: string;
    sidebarBg: string;
    inputBg: string;
    /** 边框 */
    borderLight: string;
    borderBase: string;
    borderLine: string;
    /** 代码块 */
    codeBg: string;
    codeBlockBg: string;
    tableHeaderBg: string;
  };
  radius: { sm: number; md: number; lg: number };
  shadow: { userBubble: string; bubble: string; inputUp: string };
}

const light: Palette = {
  color: {
    primary: "#2563eb",
    primaryHover: "#3b82f6",
    primaryActive: "#1d4ed8",
    primaryBg: "#eff6ff",
    primaryBorder: "#93c5fd",
    primaryText: "#1d4ed8",
    brand: "#0284c7",
    brandBg: "#e0f2fe",
    siderFrom: "#0b1220",
    siderTo: "#152238",
    textPrimary: "#1f2937",
    textSecondary: "#4b5563",
    textTertiary: "#9ca3af",
    textFaint: "#d1d5db",
    white: "#ffffff",
    contentBg: "#f8fafc",
    layoutBg: "#f1f5f9",
    sidebarBg: "#f8fafc",
    inputBg: "#f1f5f9",
    borderLight: "#e5e7eb",
    borderBase: "#eef2f7",
    borderLine: "#e5e7eb",
    codeBg: "#f1f5f9",
    codeBlockBg: "#f8fafc",
    tableHeaderBg: "#f8fafc",
  },
  radius: { sm: 8, md: 12, lg: 16 },
  shadow: {
    userBubble: "0 2px 8px rgba(37, 99, 235, 0.35)",
    bubble: "0 1px 4px rgba(15, 23, 42, 0.06)",
    inputUp: "0 -2px 12px rgba(15, 23, 42, 0.05)",
  },
};

const dark: Palette = {
  color: {
    primary: "#3b82f6",
    primaryHover: "#60a5fa",
    primaryActive: "#2563eb",
    primaryBg: "rgba(59, 130, 246, 0.16)",
    primaryBorder: "#1e40af",
    primaryText: "#93c5fd",
    brand: "#38bdf8",
    brandBg: "rgba(56, 189, 248, 0.16)",
    siderFrom: "#0b1220",
    siderTo: "#152238",
    textPrimary: "#e5e7eb",
    textSecondary: "#9ca3af",
    textTertiary: "#7c8494",
    textFaint: "#4b5563",
    white: "#161f2e",
    contentBg: "#101826",
    layoutBg: "#0d1420",
    sidebarBg: "#101826",
    inputBg: "#1a2436",
    borderLight: "#263248",
    borderBase: "#1f2a3d",
    borderLine: "#1f2a3d",
    codeBg: "#1a2436",
    codeBlockBg: "#131c2b",
    tableHeaderBg: "#1a2436",
  },
  radius: { sm: 8, md: 12, lg: 16 },
  shadow: {
    userBubble: "0 2px 8px rgba(59, 130, 246, 0.4)",
    bubble: "0 1px 4px rgba(0, 0, 0, 0.4)",
    inputUp: "0 -2px 12px rgba(0, 0, 0, 0.35)",
  },
};

export const palettes: Record<ThemeMode, Palette> = { light, dark };

export const getPalette = (mode: ThemeMode): Palette => palettes[mode];

/** antd ConfigProvider 的 theme.token（与 palette 同源） */
export function getAntdThemeToken(p: Palette) {
  return {
    colorPrimary: p.color.primary,
    colorPrimaryHover: p.color.primaryHover,
    colorPrimaryActive: p.color.primaryActive,
    colorLink: p.color.primary,
    colorLinkHover: p.color.primaryHover,
    colorPrimaryBg: p.color.primaryBg,
    colorPrimaryBorder: p.color.primaryBorder,
    colorTextHeading: p.color.textPrimary,
    borderRadius: p.radius.sm,
  };
}

/** CSS 变量名 → 值映射；主题切换时整体重注入 document.documentElement */
export function getCssVariables(mode: ThemeMode): Record<string, string> {
  const c = getPalette(mode).color;
  return {
    "--color-primary": c.primary,
    "--color-primary-hover": c.primaryHover,
    "--color-primary-active": c.primaryActive,
    "--color-primary-bg": c.primaryBg,
    "--color-primary-border": c.primaryBorder,
    "--color-primary-text": c.primaryText,
    "--color-sider-from": c.siderFrom,
    "--color-sider-to": c.siderTo,
    "--color-text-primary": c.textPrimary,
    "--color-text-secondary": c.textSecondary,
    "--color-text-tertiary": c.textTertiary,
    "--color-white": c.white,
    "--color-layout-bg": c.layoutBg,
    "--color-content-bg": c.contentBg,
    "--color-sidebar-bg": c.sidebarBg,
    "--color-input-bg": c.inputBg,
    "--color-border-light": c.borderLight,
    "--color-border-base": c.borderBase,
    "--color-code-bg": c.codeBg,
    "--color-code-block-bg": c.codeBlockBg,
    "--color-table-header-bg": c.tableHeaderBg,
  };
}
