// c4/agent/frontend/src/api/chat.ts
// POST /api/chat — web.md §3.1.1, §3.1.2, §4.2.
//
// Streams SSE from the backend. Two terminators are valid:
//   1. An explicit `event: done` record.
//   2. The ReadableStream exhausting (no more bytes).
// We MUST treat stream exhaustion as terminal — backend early-exit branches
// ("已记录场站…", "请提供场站名称…") emit text then close WITHOUT a done
// event. Waiting for done would hang the UI forever.
//
// `streamChat` resolves with the conversationId echoed by the server (via the
// X-Conversation-Id header on the first response). If the stream never set
// the header, we fall back to the conversationId the caller passed in.
//
// `truncateHistory` enforces web.md §3.1.2 限长: keep at most the most recent
// `maxRounds` user/agent exchanges (each round = 2 messages).

import { sseParser, type SseEvent } from "./sse";

export interface ChatMessage {
  role: string;
  content: string;
}

export interface ChatRequest {
  message: string;
  conversationId?: string;
  history?: ChatMessage[];
}

export interface StreamChatOptions {
  /** AbortController to cancel an in-flight stream (e.g. on unmount). */
  signal?: AbortSignal;
}

export type ChatEventHandler = (event: SseEvent) => void;

/**
 * POST /api/chat and stream SSE events back to the caller.
 *
 * @returns The echoed conversationId (X-Conversation-Id header or fallback to
 *          the caller's value, or "" if neither is set).
 */
export async function streamChat(
  req: ChatRequest,
  onEvent: ChatEventHandler,
  opts: StreamChatOptions = {},
): Promise<string> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal: opts.signal,
  });

  if (!res.ok) {
    throw new Error(`对话请求失败: HTTP ${res.status}`);
  }
  if (!res.body) {
    throw new Error("对话请求失败: 响应为空");
  }

  const conversationId =
    res.headers.get("X-Conversation-Id") ?? req.conversationId ?? "";

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE records are separated by blank lines. We split conservatively and
    // keep the trailing partial in the buffer for the next iteration.
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    if (parts.length === 0) continue;

    const chunk = parts.join("\n\n") + "\n\n";
    for (const ev of sseParser(chunk)) {
      onEvent(ev);
      if (ev.type === "done" || ev.type === "error") {
        // Either signal is terminal — release the connection.
        try {
          await reader.cancel();
        } catch {
          // ignore double-cancel
        }
        return conversationId;
      }
    }
  }

  // Stream exhausted without a `done` event. That's a valid terminal (see
  // web.md §4.2 流关闭兜底). Flush any trailing buffered partial and resolve.
  if (buffer.trim().length > 0) {
    for (const ev of sseParser(buffer + "\n\n")) onEvent(ev);
  }
  return conversationId;
}

/**
 * Limit the in-memory history we send back to the server (web.md §3.1.2).
 *
 * Backend's `express.json` body limit is 1 MB and the LLM context cost grows
 * with history size — keep only the most recent `maxRounds` exchanges.
 *
 * An "exchange" is one user message + one assistant reply, so we keep at
 * most `maxRounds * 2` trailing messages. Messages that don't have a
 * matching partner are kept if they fall inside the window.
 *
 * @param history  Full prior history (chronological order, oldest first).
 * @param maxRounds Maximum number of user/agent rounds to retain. Default 10.
 */
export function truncateHistory(
  history: ChatMessage[],
  maxRounds = 10,
): ChatMessage[] {
  const keep = Math.max(0, maxRounds) * 2;
  if (history.length <= keep) return history;
  return history.slice(history.length - keep);
}
