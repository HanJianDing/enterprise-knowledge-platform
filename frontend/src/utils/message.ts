import { message as antdMessage } from "antd";

/** antd v6 兼容层：v6 移除了静态 message.success/error/info/warning/loading
 * （只保留 message.open），全站统一收敛到这里，调用点保持旧签名不变。
 */
export const message = {
  success: (content: string, duration = 3) =>
    antdMessage.open({ type: "success", content, duration }),
  error: (content: string, duration = 3) =>
    antdMessage.open({ type: "error", content, duration }),
  info: (content: string, duration = 3) =>
    antdMessage.open({ type: "info", content, duration }),
  warning: (content: string, duration = 3) =>
    antdMessage.open({ type: "warning", content, duration }),
  loading: (content: string, duration = 0) =>
    antdMessage.open({ type: "loading", content, duration }),
  destroy: () => antdMessage.destroy(),
};
