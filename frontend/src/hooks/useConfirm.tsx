import { useCallback } from "react";
import { Modal } from "antd";
import { ExclamationCircleOutlined } from "@ant-design/icons";

interface ConfirmOptions {
  title: string;
  content?: string;
  onConfirm: () => void | Promise<void>;
  /** 是否为危险操作（删除等），会显示红色警告图标 */
  destructive?: boolean;
  /** 确认按钮文字 */
  okText?: string;
}

/**
 * 确认弹窗 hook —— 用 Ant Design Modal 封装二次确认逻辑
 *
 * 为什么不用浏览器的 confirm()？
 * 浏览器 confirm 不能自定义样式、不支持异步回调、不能显示图标。
 * 这里用 Ant Design 的 Modal.confirm，统一全站的确认弹窗风格。
 */
export function useConfirm() {
  const confirm = useCallback((opts: ConfirmOptions) => {
    Modal.confirm({
      title: opts.title,
      icon: opts.destructive ? <ExclamationCircleOutlined /> : undefined,
      content: opts.content,
      okText: opts.okText || "确定",
      cancelText: "取消",
      okButtonProps: opts.destructive ? { danger: true } : undefined,
      onOk: async () => {
        await opts.onConfirm();
      },
    });
  }, []);

  /** 删除确认的快捷方法（预设 destructive 样式） */
  const confirmDelete = useCallback(
    (title: string, onConfirm: () => void | Promise<void>) => {
      confirm({
        title: `确定删除 ${title} ？`,
        content: "删除后不可恢复",
        okText: "删除",
        destructive: true,
        onConfirm,
      });
    },
    [confirm],
  );

  return { confirm, confirmDelete };
}
