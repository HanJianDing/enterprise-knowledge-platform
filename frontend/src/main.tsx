import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { getCssVariables } from "./theme/tokens";
import { getStoredMode } from "./theme/themeStore";
import "./index.css";

// 设计 token 注入：按持久化的主题模式先注入一版 CSS 变量，
// 避免暗色用户在 React 挂载前看到白屏闪烁（App.tsx 在切换时重注入）
const initialMode = getStoredMode();
for (const [name, value] of Object.entries(getCssVariables(initialMode))) {
  document.documentElement.style.setProperty(name, value);
}
document.documentElement.style.colorScheme = initialMode;

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 30000 },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
