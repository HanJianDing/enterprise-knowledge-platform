import React, { useState } from "react";
import { Button, Modal, Input } from "antd";
import {
  CopyOutlined,
  RedoOutlined,
  LikeOutlined,
  LikeFilled,
  DislikeOutlined,
  DislikeFilled,
} from "@ant-design/icons";
import { message as showMessage } from "../../utils/message";
import { chatApi } from "../../services/chatApi";
import { useChatStore } from "../../stores/chatStore";
import { useThemeTokens } from "../../theme/themeStore";
import type { ChatMessage, FeedbackInfo } from "../../types/chat";

const { TextArea } = Input;

interface Props {
  message: ChatMessage;
  /** 仅最后一条 assistant 消息且满足条件时显示"重新生成" */
  canRegenerate: boolean;
  onRegenerate: () => void;
}

/** 复制文本：优先剪贴板 API，失败回退 textarea+execCommand（兼容 http/旧浏览器） */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  }
}

/** 消息操作条：复制 / 重新生成 / 点赞 / 点踩（点踩可带意见） */
const MessageActions: React.FC<Props> = ({ message, canRegenerate, onRegenerate }) => {
  const [dislikeOpen, setDislikeOpen] = useState(false);
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const t = useThemeTokens();

  const feedback = message.feedback;

  /** 把评价写回 store 中对应消息（done 后重拉详情也会带回，这里即时更新防闪烁） */
  const updateFeedback = (f?: FeedbackInfo) => {
    const msgs = useChatStore.getState().messages;
    useChatStore.getState().setMessages(
      msgs.map((m) => (m.id === message.id ? { ...m, feedback: f } : m))
    );
  };

  const handleCopy = async () => {
    const ok = await copyText(message.content);
    if (ok) {
      showMessage.success("已复制");
    } else {
      showMessage.error("复制失败，请手动复制");
    }
  };

  const handleLike = async () => {
    if (submitting) return;
    // 已点赞：再点 = 取消评价
    if (feedback?.rating === "like") {
      setSubmitting(true);
      try {
        await chatApi.deleteFeedback(message.id);
        updateFeedback(undefined);
      } catch {
        showMessage.error("操作失败");
      } finally {
        setSubmitting(false);
      }
      return;
    }
    setSubmitting(true);
    try {
      await chatApi.submitFeedback(message.id, "like", null);
      updateFeedback({ rating: "like", comment: null });
    } catch {
      showMessage.error("操作失败");
    } finally {
      setSubmitting(false);
    }
  };

  const handleDislikeClick = async () => {
    if (submitting) return;
    // 已点踩：再点 = 取消评价
    if (feedback?.rating === "dislike") {
      setSubmitting(true);
      try {
        await chatApi.deleteFeedback(message.id);
        updateFeedback(undefined);
      } catch {
        showMessage.error("操作失败");
      } finally {
        setSubmitting(false);
      }
      return;
    }
    setComment("");
    setDislikeOpen(true);
  };

  const submitDislike = async (finalComment: string | null) => {
    setDislikeOpen(false);
    setSubmitting(true);
    try {
      await chatApi.submitFeedback(message.id, "dislike", finalComment);
      updateFeedback({ rating: "dislike", comment: finalComment });
    } catch {
      showMessage.error("操作失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        gap: 4,
        marginTop: 6,
        opacity: 0.75,
        alignItems: "center",
      }}
    >
      <Button
        type="text"
        size="small"
        icon={<CopyOutlined />}
        onClick={handleCopy}
        title="复制回答"
      />
      {canRegenerate && (
        <Button
          type="text"
          size="small"
          icon={<RedoOutlined />}
          onClick={onRegenerate}
          title="重新生成"
        />
      )}
      <Button
        type="text"
        size="small"
        icon={
          feedback?.rating === "like" ? (
            <LikeFilled style={{ color: t.color.primary }} />
          ) : (
            <LikeOutlined />
          )
        }
        onClick={handleLike}
        title={feedback?.rating === "like" ? "取消点赞" : "有帮助"}
      />
      <Button
        type="text"
        size="small"
        danger={feedback?.rating === "dislike"}
        icon={
          feedback?.rating === "dislike" ? <DislikeFilled /> : <DislikeOutlined />
        }
        onClick={handleDislikeClick}
        title={feedback?.rating === "dislike" ? "取消点踩" : "不满意"}
      />

      {/* 点踩意见弹窗（可选填写） */}
      <Modal
        open={dislikeOpen}
        title="哪里不满意？"
        onCancel={() => setDislikeOpen(false)}
        footer={[
          <Button key="abort" onClick={() => setDislikeOpen(false)}>
            取消
          </Button>,
          <Button key="only" onClick={() => submitDislike(null)}>
            仅点踩
          </Button>,
          <Button
            key="submit"
            type="primary"
            danger
            disabled={!comment.trim()}
            onClick={() => submitDislike(comment.trim())}
          >
            提交意见
          </Button>,
        ]}
      >
        <TextArea
          rows={3}
          maxLength={500}
          autoFocus
          placeholder="选填：告诉我们哪里答得不好（如：尺码推荐错误），帮助我们改进"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
        />
      </Modal>
    </div>
  );
};

export default MessageActions;
