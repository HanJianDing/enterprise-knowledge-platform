import { create } from "zustand";
import type { Conversation, ChatMessage, SSEEvent, SSESource } from "../types/chat";

interface ChatState {
  conversations: Conversation[];
  currentConversationId: string | null;
  messages: ChatMessage[];
  isStreaming: boolean;
  streamContent: string;
  streamSources: SSEEvent["documents"];
  classifiedCategory: string | null;
  clarifiedQuestion: string | null;
  thinkingText: string | null; // 后端 thinking 事件（type:"status"）的进度提示

  setConversations: (convs: Conversation[]) => void;
  setCurrentConversation: (id: string | null) => void;
  setMessages: (msgs: ChatMessage[]) => void;
  addMessage: (msg: ChatMessage) => void;
  setIsStreaming: (v: boolean) => void;
  appendStreamContent: (content: string) => void;
  setStreamSources: (sources: SSESource[]) => void;
  setClassifiedCategory: (cat: string | null) => void;
  setClarifiedQuestion: (q: string | null) => void;
  setThinkingText: (text: string | null) => void;
  clearStream: () => void;
  /** 登出时全量清空（防共用电脑上换账号看到上一个账号的聊天残留） */
  resetChat: () => void;
}

export const useChatStore = create<ChatState>((set) => ({
  conversations: [],
  currentConversationId: null,
  messages: [],
  isStreaming: false,
  streamContent: "",
  streamSources: [],
  classifiedCategory: null,
  clarifiedQuestion: null,
  thinkingText: null,

  setConversations: (convs) => set({ conversations: convs }),
  setCurrentConversation: (id) => set({ currentConversationId: id }),
  setMessages: (msgs) => set({ messages: msgs }),
  addMessage: (msg) =>
    set((s) => ({ messages: [...s.messages, msg] })),
  setIsStreaming: (v) => set({ isStreaming: v }),
  appendStreamContent: (content) =>
    set((s) => ({ streamContent: s.streamContent + content })),
  setStreamSources: (sources) => set({ streamSources: sources || [] }),
  setClassifiedCategory: (cat) => set({ classifiedCategory: cat }),
  setClarifiedQuestion: (q) => set({ clarifiedQuestion: q }),
  setThinkingText: (text) => set({ thinkingText: text }),
  clearStream: () => set({ streamContent: "", streamSources: [], classifiedCategory: null, clarifiedQuestion: null, thinkingText: null }),
  resetChat: () =>
    set({
      conversations: [],
      currentConversationId: null,
      messages: [],
      isStreaming: false,
      streamContent: "",
      streamSources: [],
      classifiedCategory: null,
      clarifiedQuestion: null,
      thinkingText: null,
    }),
}));
