// c4/test/web/unit/sse_parser.test.ts
// L1 unit tests for the SSE parser — web.md §3.1.1, §4.2, §1.3, §3.1.3
// Reference: test plan §3.1.1–3.1.6, 3.1.8 (3.1.7 is "stream close" coverage, tested in chat_stream.test.tsx)
//
// The parser is a PURE function on a raw SSE chunk — we feed it text, assert on
// the resulting SseEvent[]. No fetch, no ReadableStream, no DOM.
//
// Public API contract (pinned):
//   export type SSEEventType =
//     | "text" | "tool_call" | "tool_result"
//     | "done" | "error" | "interrupt" | "message";
//   export interface SseEvent { type: SSEEventType; event: string|null; data: Record<string,unknown>; }
//   export function sseParser(text: string): SseEvent[];

import { describe, it, expect } from "vitest";
import { sseParser } from "@frontend/api/sse";

describe("sseParser — default data: messages (web.md §3.1.1)", () => {
  it("3.1.1 parses default data: message with type=text into a text event", () => {
    const input = `data: {"type":"text","content":"你好","conversationId":"c1"}\n\n`;
    const events = sseParser(input);
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      type: "text",
      event: null,
      data: { type: "text", content: "你好", conversationId: "c1" },
    });
  });
});

describe("sseParser — named event: lines (web.md §3.1.1, §1.3)", () => {
  it("3.1.2 parses event: done with data on next line", () => {
    const input = `event: done\ndata: {"conversationId":"c1"}\n\n`;
    const events = sseParser(input);
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      type: "done",
      event: "done",
      data: { conversationId: "c1" },
    });
  });

  it("3.1.3 parses event: error", () => {
    const input = `event: error\ndata: {"message":"服务异常","conversationId":"c1"}\n\n`;
    const events = sseParser(input);
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      type: "error",
      event: "error",
      data: { message: "服务异常", conversationId: "c1" },
    });
  });
});

describe("sseParser — keepalive and early streams (web.md §3.1.1 注意项)", () => {
  it("3.1.4 ignores :ok comment line at the head of a stream", () => {
    const input = `:ok\n\ndata: {"type":"text","content":"hi","conversationId":"c1"}\n\n`;
    const events = sseParser(input);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("text");
    expect(events[0].data).toMatchObject({ content: "hi" });
  });

  it("3.1.5 parses a stream that starts directly with data: (no :ok prefix)", () => {
    // upload's multer error branch emits text directly without keepalive
    const input = `data: {"type":"text","content":"x","conversationId":"c1"}\n\n`;
    const events = sseParser(input);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("text");
  });
});

describe("sseParser — tool_call invariant (web.md §3.1.1 注意项)", () => {
  it("3.1.6 tool_call args is always an empty object {}", () => {
    const input = `data: {"type":"tool_call","name":"xlsx_parser","args":{},"conversationId":"c1"}\n\n`;
    const events = sseParser(input);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("tool_call");
    expect(events[0].data).toMatchObject({
      type: "tool_call",
      name: "xlsx_parser",
      args: {},
      conversationId: "c1",
    });
    expect((events[0].data as Record<string, unknown>).args).toEqual({});
  });
});

describe("sseParser — interrupt negative guard (web.md §1.3, §3.1.3)", () => {
  it("3.1.8 parses event: interrupt without crashing and surfaces it as type='interrupt'", () => {
    // The backend declares `interrupt` but never emits it. Parser must:
    //   (a) parse it safely when present (no throw),
    //   (b) expose it under type='interrupt' so any downstream code can ignore it.
    const input = `event: interrupt\ndata: {"message":"请确认","interruptId":"x1","conversationId":"c1"}\n\n`;
    expect(() => sseParser(input)).not.toThrow();
    const events = sseParser(input);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("interrupt");
    expect(events[0].event).toBe("interrupt");
  });
});

describe("sseParser — multi-event chunk & default fallback (web.md §4.2)", () => {
  it("parses a chunk containing multiple events separated by blank lines", () => {
    const input =
      `:ok\n\n` +
      `data: {"type":"text","content":"a","conversationId":"c1"}\n\n` +
      `data: {"type":"text","content":"b","conversationId":"c1"}\n\n` +
      `event: done\ndata: {"conversationId":"c1"}\n\n`;
    const events = sseParser(input);
    expect(events).toHaveLength(3);
    expect(events.map((e) => e.type)).toEqual(["text", "text", "done"]);
  });

  it("defaults a default data: line without a `type` field to type='message'", () => {
    const input = `data: {"hello":"world"}\n\n`;
    const events = sseParser(input);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("message");
    expect(events[0].event).toBeNull();
  });
});
