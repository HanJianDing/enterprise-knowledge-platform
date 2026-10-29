import { message } from "../utils/message";
import { useCallback } from "react";

/**
 * 统一的消息提示 hook
 *
 * 对 antd message 做一层薄封装：
 * - 统一默认配置（3秒自动消失）
 * - 后续可以在这里加埋点、统一替换通知方式等
 */
export function useMessage() {
  const showSuccess = useCallback((text: string) => {
    message.success(text, 3);
  }, []);

  const showError = useCallback((text: string) => {
    message.error(text, 3);
  }, []);

  const showInfo = useCallback((text: string) => {
    message.info(text, 3);
  }, []);

  const showLoading = useCallback((text: string) => {
    return message.loading(text, 0); // 不自动消失，需要手动关闭
  }, []);

  return { showSuccess, showError, showInfo, showLoading };
}
