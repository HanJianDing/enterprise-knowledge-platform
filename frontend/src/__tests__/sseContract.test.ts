import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { chatApi } from "../services/chatApi";
import type { SSEEvent } from "../types/chat";

/** 构造 SSE 流式 fetch mock */
function mockSseFetch(lines: string[]) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const line of lines) {
        controller.enqueue(encoder.encode(line));
      }
      controller.close();
    },
  });
  return vi.spyOn(globalThis, "fetch").mockResolvedValue({
    ok: true,
    body: stream,
  } as any);
}

/** 消费一次 streamChat，收集事件 */
function collectEvents(): Promise<SSEEvent[]> {
  const events: SSEEvent[] = [];
  return new Promise((resolve) => {
    chatApi.streamChat(
      "年假怎么申请？",
      null,
      "user",
      (e) => events.push(e),
      () => resolve(events),
      () => resolve(events)
    );
  });
}

describe("SSE 契约（企业知识库改造阶段2：移除 transfer/queued，员工端来源带原文）", () => {
  beforeEach(() => {
    localStorage.setItem("access_token", "test_token");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("classified 事件载荷内嵌 type，前端 switch 可识别", async () => {
    mockSseFetch([
      'event: classified\n',
      'data: {"type":"classified","category_id":"cat1","category_name":"年假政策","clarified_question":"年假政策 申请条件"}\n\n',
      'event: done\n',
      'data: {"type":"done","conversation_id":"c1","message_id":"m1"}\n\n',
    ]);

    const events = await collectEvents();
    expect(events).toHaveLength(2);
    expect(events[0]!.type).toBe("classified");
    expect(events[0]!.category_name).toBe("年假政策");
    expect(events[1]!.type).toBe("done");
    expect(events[1]!.conversation_id).toBe("c1");
  });

  it("sources 事件对员工也携带原文片段（答案要有出处可查证）", async () => {
    mockSseFetch([
      'event: sources\n',
      'data: {"type":"sources","documents":[{"index":1,"doc_id":"d1","title":"年假制度.txt","chunk_idx":0,"text":"入职满一年可享受5天带薪年假","category_path":"休假制度 > 年假政策"}]}\n\n',
    ]);

    const events = await collectEvents();
    expect(events).toHaveLength(1);
    const source = events[0]!.documents![0]!;
    expect(source.title).toBe("年假制度.txt");
    expect(source.text).toContain("5天带薪年假");
  });

  it("thinking 进度事件（event 名 thinking，载荷 type=status）", async () => {
    mockSseFetch([
      'event: thinking\n',
      'data: {"type":"status","message":"正在理解问题并检索知识库..."}\n\n',
    ]);

    const events = await collectEvents();
    expect(events).toHaveLength(1);
    expect(events[0]!.type).toBe("status");
    expect(events[0]!.message).toBe("正在理解问题并检索知识库...");
  });
});
