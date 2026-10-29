import { describe, it, expect } from "vitest";
import { palettes, getPalette, getCssVariables, getAntdThemeToken } from "../theme/tokens";

describe("主题调色板", () => {
  it("light 与 dark 调色板键一一对应（换肤不丢键、不漏键）", () => {
    const lightKeys = Object.keys(palettes.light.color).sort();
    const darkKeys = Object.keys(palettes.dark.color).sort();
    expect(darkKeys).toEqual(lightKeys);
  });

  it("两种模式的 CSS 变量集合一致", () => {
    const lightVars = Object.keys(getCssVariables("light")).sort();
    const darkVars = Object.keys(getCssVariables("dark")).sort();
    expect(darkVars).toEqual(lightVars);
  });

  it("antd token 与调色板同源", () => {
    const token = getAntdThemeToken(getPalette("light"));
    expect(token.colorPrimary).toBe(palettes.light.color.primary);
  });
});
