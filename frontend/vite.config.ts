import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 后端地址可配（演示环境用 8010）：VITE_BACKEND_URL=http://localhost:8010 npm run dev
const backendTarget = process.env.VITE_BACKEND_URL || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: backendTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    // echarts 单独成 chunk：第三方库独立缓存 + 观察按需引入后的真实体积
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes("node_modules/echarts") || id.includes("node_modules/zrender")) {
            return "echarts";
          }
        },
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./src/test-setup.ts",
    css: true,
    // 批次F 前置·diff 视图：antd List/多异步渲染的组件测试超过默认 5s
    // 超时（高负载机器尤甚）——统一放宽到 15s，不改变断言语义
    testTimeout: 15000,
  },
});
