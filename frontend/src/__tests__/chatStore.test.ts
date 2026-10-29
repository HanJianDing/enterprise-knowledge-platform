import { describe, it, expect, beforeEach } from "vitest";
import { useChatStore } from "../stores/chatStore";

describe("chatStore thinking 进度状态", () => {
  beforeEach(() => {
    useChatStore.setState({
      thinkingText: null,
      streamContent: "abc",
      streamSources: [],
      classifiedCategory: "分类",
      clarifiedQuestion: "检索词",
    });
  });

  it("setThinkingText 更新进度提示", () => {
    useChatStore.getState().setThinkingText("正在检索...");
    expect(useChatStore.getState().thinkingText).toBe("正在检索...");
  });

  it("clearStream 一并清空 thinking（停止/发送/切会话时的状态一致性）", () => {
    useChatStore.getState().setThinkingText("正在检索...");
    useChatStore.getState().clearStream();
    expect(useChatStore.getState().thinkingText).toBeNull();
    expect(useChatStore.getState().streamContent).toBe("");
    expect(useChatStore.getState().classifiedCategory).toBeNull();
  });
});
