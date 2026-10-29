import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import MessageActions from "../components/chat/MessageActions";
import { useChatStore } from "../stores/chatStore";
import type { ChatMessage } from "../types/chat";

const fakeMsg: ChatMessage = {
  id: "m1",
  conversation_id: "c1",
  role: "assistant",
  content: "回答内容",
  created_at: "2026-09-03T00:00:00Z",
};

const mockSubmitFeedback = vi.fn();
const mockDeleteFeedback = vi.fn();

vi.mock("../services/chatApi", () => ({
  chatApi: {
    submitFeedback: (...args: unknown[]) => mockSubmitFeedback(...args),
    deleteFeedback: (...args: unknown[]) => mockDeleteFeedback(...args),
  },
}));

vi.mock("../utils/message", () => ({
  message: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
    loading: vi.fn(),
    destroy: vi.fn(),
  },
}));

describe("MessageActions", () => {
  beforeEach(() => {
    mockSubmitFeedback.mockClear();
    mockDeleteFeedback.mockClear();
    mockSubmitFeedback.mockResolvedValue({ rating: "like", comment: null });
    mockDeleteFeedback.mockResolvedValue({});
    useChatStore.setState({ messages: [fakeMsg] });
  });

  it("复制按钮点击后写入剪贴板", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    // navigator.clipboard 在 jsdom 是只读 getter，需 defineProperty 覆盖
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    render(
      <MessageActions message={fakeMsg} canRegenerate={false} onRegenerate={vi.fn()} />
    );
    await user.click(screen.getByTitle("复制回答"));
    expect(writeText).toHaveBeenCalledWith("回答内容");
  });

  it("canRegenerate=true 显示重新生成按钮并触发回调", async () => {
    const user = userEvent.setup();
    const onRegenerate = vi.fn();
    render(
      <MessageActions message={fakeMsg} canRegenerate={true} onRegenerate={onRegenerate} />
    );
    await user.click(screen.getByTitle("重新生成"));
    expect(onRegenerate).toHaveBeenCalled();
  });

  it("canRegenerate=false 不显示重新生成按钮", () => {
    render(
      <MessageActions message={fakeMsg} canRegenerate={false} onRegenerate={vi.fn()} />
    );
    expect(screen.queryByTitle("重新生成")).toBeNull();
  });

  it("点赞：调接口并把评价写回 store", async () => {
    const user = userEvent.setup();
    render(
      <MessageActions message={fakeMsg} canRegenerate={false} onRegenerate={vi.fn()} />
    );
    await user.click(screen.getByTitle("有帮助"));
    expect(mockSubmitFeedback).toHaveBeenCalledWith("m1", "like", null);

    const msgs = useChatStore.getState().messages;
    expect(msgs[0]?.feedback).toEqual({ rating: "like", comment: null });
  });

  it("已点赞再点 = 取消评价（DELETE）", async () => {
    const user = userEvent.setup();
    const likedMsg: ChatMessage = {
      ...fakeMsg,
      feedback: { rating: "like", comment: null },
    };
    useChatStore.setState({ messages: [likedMsg] });
    render(
      <MessageActions message={likedMsg} canRegenerate={false} onRegenerate={vi.fn()} />
    );
    await user.click(screen.getByTitle("取消点赞"));
    expect(mockDeleteFeedback).toHaveBeenCalledWith("m1");

    const msgs = useChatStore.getState().messages;
    expect(msgs[0]?.feedback).toBeUndefined();
  });

  it("点踩弹意见框：仅点踩 → 提交 dislike 无意见", async () => {
    const user = userEvent.setup();
    render(
      <MessageActions message={fakeMsg} canRegenerate={false} onRegenerate={vi.fn()} />
    );
    await user.click(screen.getByTitle("不满意"));
    expect(screen.getByText("哪里不满意？")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "仅点踩" }));
    expect(mockSubmitFeedback).toHaveBeenCalledWith("m1", "dislike", null);

    const msgs = useChatStore.getState().messages;
    expect(msgs[0]?.feedback).toEqual({ rating: "dislike", comment: null });
  });

  it("点踩填意见：提交意见按钮携带文字", async () => {
    const user = userEvent.setup();
    render(
      <MessageActions message={fakeMsg} canRegenerate={false} onRegenerate={vi.fn()} />
    );
    await user.click(screen.getByTitle("不满意"));
    await user.type(
      screen.getByPlaceholderText(/告诉我们哪里答得不好/),
      "尺码推荐错误"
    );
    await user.click(screen.getByRole("button", { name: "提交意见" }));
    expect(mockSubmitFeedback).toHaveBeenCalledWith("m1", "dislike", "尺码推荐错误");

    const msgs = useChatStore.getState().messages;
    expect(msgs[0]?.feedback).toEqual({
      rating: "dislike",
      comment: "尺码推荐错误",
    });
  });

  it("点踩弹窗点取消 = 不提交任何评价", async () => {
    const user = userEvent.setup();
    render(
      <MessageActions message={fakeMsg} canRegenerate={false} onRegenerate={vi.fn()} />
    );
    await user.click(screen.getByTitle("不满意"));
    // antd 会给两个汉字间插空格（"取 消"），用正则匹配
    await user.click(screen.getByRole("button", { name: /取\s*消/ }));
    expect(mockSubmitFeedback).not.toHaveBeenCalled();
    expect(mockDeleteFeedback).not.toHaveBeenCalled();
  });
});
