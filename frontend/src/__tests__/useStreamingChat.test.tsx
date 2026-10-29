// 质检补测（550dfd3）：error 事件展示后端可读提示——此前被静默吞掉
import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useStreamingChat } from "../hooks/useStreamingChat";

const messageError = vi.fn();
vi.mock("../utils/message", () => ({
  message: {
    success: vi.fn(), error: (...a: unknown[]) => messageError(...a),
    info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(),
  },
}));

// 可变 mock store 状态
let mockStore: Record<string, unknown> = {};
const storeFns = {
  setIsStreaming: vi.fn(),
  appendStreamContent: vi.fn(),
  setStreamSources: vi.fn(),
  setClassifiedCategory: vi.fn(),
  setClarifiedQuestion: vi.fn(),
  setThinkingText: vi.fn(),
  clearStream: vi.fn(),
  addMessage: vi.fn(),
  setCurrentConversation: vi.fn(),
};
vi.mock("../stores/chatStore", () => ({
  useChatStore: vi.fn(() => ({
    isStreaming: false,
    streamContent: "",
    streamSources: [],
    messages: [],
    currentConversationId: null,
    ...storeFns,
    ...mockStore,
  })),
}));

vi.mock("../stores/authStore", () => ({
  useAuthStore: Object.assign(
    vi.fn(() => ({ isAdmin: false })),
    { getState: vi.fn(() => ({ isAdmin: false })) },
  ),
}));

// streamChat：捕获 SSE handler，返回假 controller
let capturedHandler: ((event: unknown) => void) | null = null;
const mockStreamChat = vi.fn(
  (_text: string, _conv: string | null, _role: string,
    handler: (event: unknown) => void, _onError: (e: unknown) => void,
    _onDone: () => void) => {
    capturedHandler = handler;
    return { abort: vi.fn() };
  },
);
vi.mock("../services/chatApi", () => ({
  chatApi: {
    streamChat: (...a: unknown[]) =>
      mockStreamChat(...(a as [string, string | null, string, never, never, never])),
    getConversation: vi.fn(() => Promise.resolve({ messages: [] })),
    regenerate: vi.fn(),
  },
}));

describe("useStreamingChat error 事件（550dfd3 补测）", () => {
  beforeEach(() => {
    messageError.mockClear();
    mockStreamChat.mockClear();
    mockStore = {};
    capturedHandler = null;
  });

  it("error 事件展示后端可读提示（不再静默吞掉）", async () => {
    const { result } = renderHook(() => useStreamingChat());
    await act(async () => {
      result.current.sendMessage("年假怎么申请");
    });
    expect(mockStreamChat).toHaveBeenCalledTimes(1);
    expect(capturedHandler).not.toBeNull();

    await act(async () => {
      capturedHandler!({
        type: "error",
        message: "生成回答时出错，请稍后重试；若问题持续，可联系知识库管理员",
      });
    });
    expect(messageError).toHaveBeenCalledWith(
      "生成回答时出错，请稍后重试；若问题持续，可联系知识库管理员",
    );
  });

  it("error 事件无 message 时给兜底文案", async () => {
    const { result } = renderHook(() => useStreamingChat());
    await act(async () => {
      result.current.sendMessage("测试");
    });
    await act(async () => {
      capturedHandler!({ type: "error" });
    });
    expect(messageError).toHaveBeenCalledWith("生成回答时出错，请稍后重试");
  });
});
