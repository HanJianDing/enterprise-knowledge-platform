import React, { useEffect, useState } from "react";
import { Input, Button } from "antd";
import { SendOutlined, StopOutlined } from "@ant-design/icons";
import { useThemeTokens } from "../../theme/themeStore";
import { message } from "../../utils/message";

const { TextArea } = Input;

/** 与后端 ChatRequest.message max_length 一致（上线安全修复：防超长消息烧 token） */
export const MESSAGE_MAX_LEN = 2000;

interface Props {
  onSend: (message: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /** 流式生成中：发送按钮切换为"停止生成"按钮 */
  isStreaming?: boolean;
  onStop?: () => void;
  draft?: { text: string; key: string | number };
}

const ChatInput: React.FC<Props> = ({
  onSend,
  disabled,
  placeholder = "输入您的问题...",
  isStreaming = false,
  onStop,
  draft,
}) => {
  const [value, setValue] = useState("");
  const t = useThemeTokens();
  useEffect(() => { if (draft) setValue(draft.text.slice(0, MESSAGE_MAX_LEN)); }, [draft]);

  const handleSend = () => {
    if (!value.trim() || disabled || isStreaming) return;
    if (value.trim().length > MESSAGE_MAX_LEN) {
      message.error(`消息最长 ${MESSAGE_MAX_LEN} 字`);
      return;
    }
    onSend(value.trim());
    setValue("");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div style={{ display: "flex", gap: 10, alignItems: "flex-end" }}>
      <TextArea
        aria-label="输入问题"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={isStreaming ? "正在生成回答..." : placeholder}
        autoSize={{ minRows: 2, maxRows: 5 }}
        maxLength={MESSAGE_MAX_LEN}
        showCount={{ formatter: ({ count }) => `${count}/${MESSAGE_MAX_LEN}` }}
        disabled={disabled || isStreaming}
        style={{
          flex: 1,
          borderRadius: 12,
          padding: "10px 14px",
          fontSize: 14,
          background: t.color.inputBg,
          border: `1px solid ${t.color.borderLight}`,
        }}
      />
      {isStreaming ? (
        <Button
          danger
          icon={<StopOutlined />}
          onClick={onStop}
          size="large"
          style={{
            borderRadius: 12,
            width: 44,
            height: 44,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 0,
          }}
          title="停止生成"
          aria-label="停止生成"
        />
      ) : (
        <Button
          type="primary"
          aria-label="发送问题"
          icon={<SendOutlined />}
          onClick={handleSend}
          disabled={disabled || !value.trim()}
          size="large"
          style={{
            borderRadius: 12,
            width: 44,
            height: 44,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 0,
          }}
        />
      )}
    </div>
  );
};

export default ChatInput;
