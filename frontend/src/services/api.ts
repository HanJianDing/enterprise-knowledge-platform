import axios from "axios";
import { useAuthStore } from "../stores/authStore";

const api = axios.create({
  baseURL: "/api/v1",
  timeout: 60000,
  headers: { "Content-Type": "application/json" },
});

// 请求拦截器: 自动添加 JWT
api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().accessToken;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// ── 单飞刷新（上线安全修复）──
// token 过期瞬间页面并发的 N 个请求各自拿到 401 时，共享同一个刷新请求，
// 避免 N 次重复刷新（防刷新风暴）；SSE 流式请求（裸 fetch，不走本拦截器）
// 也复用这个入口统一走"刷新→重试/登出"流程。
let refreshPromise: Promise<boolean> | null = null;

/** 刷新 access token（并发安全：同一时刻只发一次刷新请求）。
 *  成功 → 更新 store 并返回 true；失败 → 记录顶号提示（如有）、登出、返回 false。 */
export async function refreshAccessToken(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      const refreshToken = useAuthStore.getState().refreshToken;
      if (!refreshToken) {
        useAuthStore.getState().logout();
        return false;
      }
      try {
        // 用裸 axios 发刷新请求：不经过本拦截器，避免刷新自身 401 时递归
        const res = await axios.post("/api/v1/auth/refresh", {
          refresh_token: refreshToken,
        });
        const { access_token } = res.data;
        useAuthStore.getState().setTokens(access_token, refreshToken);
        return true;
      } catch (refreshError: unknown) {
        // 顶号场景：refresh 401 的 detail 是"账号已在其他设备登录"
        const detail = (refreshError as {
          response?: { data?: { detail?: unknown } };
        })?.response?.data?.detail;
        if (typeof detail === "string" && detail.includes("其他设备")) {
          useAuthStore.getState().setKickedMessage(detail);
        }
        useAuthStore.getState().logout();
        return false;
      }
    })().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

// 响应拦截器: 处理 401 自动刷新
// 安全修复: refresh token 走请求体（查询参数会落进代理/访问日志）；
// _retry 标记防止"刷新成功但接口仍 401"时无限重试风暴。
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (error.response?.status === 401 && !error.config?._retry) {
      const ok = await refreshAccessToken();
      if (ok) {
        error.config._retry = true;
        error.config.headers.Authorization = `Bearer ${useAuthStore.getState().accessToken}`;
        return api.request(error.config);
      }
    }
    return Promise.reject(error);
  }
);

export default api;
