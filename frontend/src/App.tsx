import { useEffect } from "react";
import { useRoutes } from "react-router-dom";
import { ConfigProvider, theme as antdTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { routes } from "./routes";
import { getAntdThemeToken, getCssVariables, getPalette } from "./theme/tokens";
import { useThemeStore } from "./theme/themeStore";

function App() {
  const element = useRoutes(routes);
  const mode = useThemeStore((s) => s.mode);

  // 模式切换：重注入 CSS 变量 + 同步原生控件配色（滚动条/表单控件）
  useEffect(() => {
    for (const [name, value] of Object.entries(getCssVariables(mode))) {
      document.documentElement.style.setProperty(name, value);
    }
    document.documentElement.style.colorScheme = mode;
  }, [mode]);

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm:
          mode === "dark" ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: getAntdThemeToken(getPalette(mode)),
      }}
    >
      {element}
    </ConfigProvider>
  );
}

export default App;
