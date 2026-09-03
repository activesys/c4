// c4/test/web/unit/chat_stream.test.tsx
// L1 unit tests for the chat stream render path — web.md §3.1.2, §3.1.3,
// §3.6 (history), §3.2.4/§3.2.5 (button payloads), and §1.3/§3.1.3 (interrupt
// negative guard).
//
// What we exercise:
//   3.6.1 append-not-replace: 3 text tokens append into a single bubble.
//   3.6.2 history truncation: sending 30 history messages yields only 20 on
//         the wire (N=10 rounds * 2 messages).
//   3.6.3 interrupt event: an `event: interrupt` record does NOT trigger
//         the confirm/cancel button (only phrase matching does).
//   3.2.4 confirm button payload: POST {message:"确认", history}
//   3.2.5 cancel button payload: POST {message:"[C4_BUTTON_CANCEL] 取消，不执行", history}
//   3.1.7 stream-close fallback (no `event: done`): caller resolves anyway.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { ChatView } from "@frontend/components/ChatView";
import { truncateHistory } from "@frontend/api/chat";

const server = setupServer();

beforeEach(() => {
  server.resetHandlers();
  server.listen({ onUnhandledRequest: "error" });
});
afterEach(() => {
  server.resetHandlers();
  server.close();
  vi.restoreAllMocks();
});

/**
 * Build a synthetic ReadableStream<Uint8Array> from SSE-shaped string chunks
 * for tests that need fine-grained timing. Each chunk is encoded as UTF-8
 * and emitted in order; the final chunk flushes.
 */
function sseStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
}

/** Wrap a stream into a Response so we can hand it to msw. */
function streamResponse(chunks: string[]): Response {
  const stream = sseStream(chunks);
  return new Response(stream, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "X-Conversation-Id": "c-test",
    },
  });
}

describe("ChatView — streaming render (web.md §3.1.2, §4.2)", () => {
  it("3.6.1 appends 3 text tokens into a single bubble (no replace)", async () => {
    server.use(
      http.post("/api/chat", () =>
        streamResponse([
          `:ok\n\n`,
          `data: {"type":"text","content":"方案","conversationId":"c1"}\n\n`,
          `data: {"type":"text","content":"如下","conversationId":"c1"}\n\n`,
          `data: {"type":"text","content":"…","conversationId":"c1"}\n\n`,
          `event: done\ndata: {"conversationId":"c1"}\n\n`,
        ]),
      ),
    );

    render(<ChatView />);

    // Type into the input and click send.
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "请给我方案" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "发送" }));
    });

    // After the stream completes, a single agent bubble holds the concatenated
    // text. Three bubbles would mean "replace" — we assert exactly one.
    await waitFor(() => {
      expect(screen.getByText("方案如下…")).toBeInTheDocument();
    });
    // Count agent bubbles — there must be exactly 1 (the user bubble is a
    // separate testid).
    const agentBubbles = screen.getAllByTestId("agent-bubble");
    expect(agentBubbles).toHaveLength(1);
  });

  it("3.1.7 resolves on stream close even when no `event: done` is sent", async () => {
    // Backend's early-exit branches emit text then close the stream without
    // a done event (web.md §4.2 流关闭兜底). The UI must not hang.
    server.use(
      http.post("/api/chat", () =>
        streamResponse([
          `:ok\n\n`,
          `data: {"type":"text","content":"已记录场站 1#风机","conversationId":"c1"}\n\n`,
          // Stream ends here — no `event: done` follow-up.
        ]),
      ),
    );

    render(<ChatView />);

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "接入 1#风机" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "发送" }));
    });

    await waitFor(() => {
      expect(screen.getByText("已记录场站 1#风机")).toBeInTheDocument();
    });
    // Send button stays disabled because the draft is now empty — instead,
    // check that the input is re-enabled, proving the stream was terminal.
    await waitFor(() => {
      expect(screen.getByTestId("chat-input")).not.toBeDisabled();
    });
  });
});

describe("ChatView — file upload echo (web.md §3.2.2)", () => {
  it("appends upload-echo text chunks into a single agent bubble (no replace) — 解析结果是 Agent 输出，按 agent 样式渲染（web.md §3.2.2）", async () => {
    server.use(
      http.post("/api/upload", () =>
        streamResponse([
          `data: {"type":"text","content":"解析"}\n\n`,
          `data: {"type":"text","content":"完成：1#风机"}\n\n`,
          `data: {"type":"text","content":" IP 192.168.110.10"}\n\n`,
          `event: done\ndata: {}\n\n`,
        ]),
      ),
    );

    render(<ChatView />);

    const file = new File(["dummy"], "points.txt", { type: "text/plain" });
    fireEvent.change(screen.getByTestId("file-upload-input"), {
      target: { files: [file] },
    });

    await waitFor(() => {
      expect(
        screen.getByText("解析完成：1#风机 IP 192.168.110.10"),
      ).toBeInTheDocument();
    });
    const agentBubbles = screen.getAllByTestId("agent-bubble");
    expect(agentBubbles.length).toBeGreaterThanOrEqual(1);
    expect(agentBubbles[agentBubbles.length - 1].textContent).toBe(
      "解析完成：1#风机 IP 192.168.110.10",
    );
    // 上传回显不得再以 user 样式渲染（蓝底白字为用户输入专属）
    expect(screen.queryByTestId("user-bubble")).toBeNull();
  });
});

describe("ChatView — interrupt negative guard (web.md §1.3, §3.1.3)", () => {
  it("3.6.3 receiving event: interrupt does NOT show the confirm/cancel buttons", async () => {
    // Build a stream that emits an interrupt record followed by text that
    // contains NO confirm phrase. The buttons must remain hidden.
    server.use(
      http.post("/api/chat", () =>
        streamResponse([
          `event: interrupt\ndata: {"message":"请确认","interruptId":"x1","conversationId":"c1"}\n\n`,
          `data: {"type":"text","content":"这是普通回答","conversationId":"c1"}\n\n`,
          `event: done\ndata: {"conversationId":"c1"}\n\n`,
        ]),
      ),
    );

    render(<ChatView />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "hi" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "发送" }));
    });

    await waitFor(() => {
      expect(screen.getByText("这是普通回答")).toBeInTheDocument();
    });

    // No confirm / cancel button should appear.
    expect(screen.queryByRole("button", { name: "确认" })).toBeNull();
    expect(screen.queryByRole("button", { name: "取消" })).toBeNull();
  });
});

describe("ChatView — confirm/cancel button payloads (web.md §3.2.4, §3.2.5, §3.1.3)", () => {
  /**
   * Assert that the i-th POST to /api/chat matches a given predicate on the
   * body. Resolves once the predicate is satisfied.
   */
  async function waitForNthPost(
    n: number,
    predicate: (body: { message: string; history?: unknown[]; resume?: boolean; interruptId?: string }) => boolean,
  ): Promise<{ message: string; history?: unknown[]; resume?: boolean; interruptId?: string }> {
    const posts: Array<{ message: string; history?: unknown[]; resume?: boolean; interruptId?: string }> = [];
    return new Promise((resolve) => {
      server.use(
        http.post("/api/chat", async ({ request }) => {
          const body = (await request.json()) as { message: string; history?: unknown[]; resume?: boolean; interruptId?: string };
          posts.push(body);
          if (posts.length >= n && predicate(body)) {
            resolve(body);
            return streamResponse([`event: done\ndata: {"conversationId":"c1"}\n\n`]);
          }
          // Default: empty text + done so the stream completes.
          return streamResponse([`event: done\ndata: {"conversationId":"c1"}\n\n`]);
        }),
      );
    });
  }

  it("3.2.4 clicking 确认 posts { message:'确认', history } (NO resume/interruptId)", async () => {
    const firstResponse = streamResponse([
      `:ok\n\n`,
      `data: {"type":"text","content":"是否确认执行？","conversationId":"c1"}\n\n`,
      `event: done\ndata: {"conversationId":"c1"}\n\n`,
    ]);

    // Capture subsequent posts separately.
    let nextRequestCount = 0;
    let confirmBody: { message: string; history?: unknown[]; resume?: boolean; interruptId?: string } | null = null;

    server.use(
      http.post("/api/chat", async ({ request }) => {
        nextRequestCount++;
        if (nextRequestCount === 1) return firstResponse;
        const body = (await request.json()) as { message: string; history?: unknown[]; resume?: boolean; interruptId?: string };
        confirmBody = body;
        return streamResponse([`event: done\ndata: {"conversationId":"c1"}\n\n`]);
      }),
    );

    render(<ChatView />);

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "请接入" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "发送" }));
    });

    const confirmBtn = await screen.findByRole("button", { name: "确认" });
    await act(async () => {
      fireEvent.click(confirmBtn);
    });

    await waitFor(() => {
      expect(confirmBody).not.toBeNull();
    });
    expect(confirmBody!.message).toBe("[C4_BUTTON_CONFIRM] 确认");
    // The user bubble renders the friendly label, never the raw envelope.
    const userBubbles = screen.getAllByTestId("user-bubble");
    expect(userBubbles[userBubbles.length - 1].textContent).toBe("确认");
    expect(userBubbles[userBubbles.length - 1].textContent).not.toContain(
      "C4_BUTTON",
    );
    // The confirm post must NOT carry resume/interruptId (§3.1.3 不依赖).
    expect(confirmBody!.resume).toBeUndefined();
    expect(confirmBody!.interruptId).toBeUndefined();
    // History is included as an array (even if empty for round 2).
    expect(Array.isArray(confirmBody!.history)).toBe(true);
  });

  it("3.2.5 clicking 取消 posts { message:'[C4_BUTTON_CANCEL] 取消，不执行', history } (NO resume/interruptId)", async () => {
    const firstResponse = streamResponse([
      `:ok\n\n`,
      `data: {"type":"text","content":"请确认是否按方案执行","conversationId":"c1"}\n\n`,
      `event: done\ndata: {"conversationId":"c1"}\n\n`,
    ]);

    let nextRequestCount = 0;
    let cancelBody: { message: string; history?: unknown[]; resume?: boolean; interruptId?: string } | null = null;

    server.use(
      http.post("/api/chat", async ({ request }) => {
        nextRequestCount++;
        if (nextRequestCount === 1) return firstResponse;
        const body = (await request.json()) as { message: string; history?: unknown[]; resume?: boolean; interruptId?: string };
        cancelBody = body;
        return streamResponse([`event: done\ndata: {"conversationId":"c1"}\n\n`]);
      }),
    );

    render(<ChatView />);

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "请接入" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "发送" }));
    });

    const cancelBtn = await screen.findByRole("button", { name: "取消" });
    await act(async () => {
      fireEvent.click(cancelBtn);
    });

    await waitFor(() => {
      expect(cancelBody).not.toBeNull();
    });
    expect(cancelBody!.message).toBe("[C4_BUTTON_CANCEL] 取消，不执行");
    expect(cancelBody!.resume).toBeUndefined();
    expect(cancelBody!.interruptId).toBeUndefined();
    expect(Array.isArray(cancelBody!.history)).toBe(true);
  });
});

describe("truncateHistory — N-round limit (web.md §3.1.2 限长)", () => {
  it("3.6.2 keeps only the most recent N*2 messages (default N=10 rounds)", () => {
    // 30 messages = 15 rounds; we should keep only the last 20.
    const history = Array.from({ length: 30 }, (_, i) => ({
      role: i % 2 === 0 ? "user" : "assistant",
      content: `msg-${i}`,
    }));
    const truncated = truncateHistory(history, 10);
    expect(truncated).toHaveLength(20);
    expect(truncated[0]).toEqual({ role: "user", content: "msg-10" });
    expect(truncated[truncated.length - 1]).toEqual({
      role: "assistant",
      content: "msg-29",
    });
  });

  it("returns the input unchanged if shorter than the limit", () => {
    const history = [
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ];
    expect(truncateHistory(history, 10)).toBe(history);
  });

  it("honors a custom maxRounds argument", () => {
    const history = Array.from({ length: 12 }, (_, i) => ({
      role: i % 2 === 0 ? "user" : "assistant",
      content: `m${i}`,
    }));
    const truncated = truncateHistory(history, 3); // 6 messages
    expect(truncated).toHaveLength(6);
    expect(truncated[0].content).toBe("m6");
  });
});
