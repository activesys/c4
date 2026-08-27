// c4/agent/frontend/src/hooks/useChatStream.ts
// Chat-stream state machine — web.md §3.1.2, §4.2.
//
// Tracks the in-flight stream and exposes a small reactive surface:
//   - status: idle | sending | streaming | error
//   - messages: rendered bubbles (user + agent)
//   - toolCards: in-progress / completed tool calls
//   - assistantText: accumulated text of the current agent bubble (for
//     confirm-phrase detection)
//
// Stream-termination rules (web.md §4.2):
//   - `event: done` → terminal
//   - `event: error` → terminal + error message bubble
//   - ReadableStream exhaustion → terminal (no `done` needed)
//
// Interrupt events (§1.3) are received but NOT surfaced — the design
// declares interrupt as "backend never emits it", so the UI does not depend
// on it. We still tolerate it gracefully (no crash).

import { useCallback, useRef, useState } from "react";
import {
  streamChat,
  truncateHistory,
  type ChatMessage,
} from "@frontend/api/chat";
import type { SseEvent } from "@frontend/api/sse";

export type ChatStreamStatus =
  | "idle"
  | "sending"
  | "streaming"
  | "error";

export interface ToolCardState {
  name: string;
  status: "running" | "done";
  result?: string;
}

export interface ChatBubble {
  id: string;
  role: "user" | "agent" | "error";
  content: string;
}

export interface UseChatStreamReturn {
  status: ChatStreamStatus;
  messages: ChatBubble[];
  toolCards: ToolCardState[];
  assistantText: string;
  error: string | null;
  send: (
    message: string,
    history?: ChatMessage[],
    conversationId?: string,
  ) => Promise<void>;
  streamEcho: (content: string) => void;
  endEcho: () => void;
  reset: () => void;
}

let bubbleSeq = 0;
function nextId(prefix: string): string {
  bubbleSeq += 1;
  return `${prefix}-${bubbleSeq}`;
}

export function useChatStream(): UseChatStreamReturn {
  const [status, setStatus] = useState<ChatStreamStatus>("idle");
  const [messages, setMessages] = useState<ChatBubble[]>([]);
  const [toolCards, setToolCards] = useState<ToolCardState[]>([]);
  const [assistantText, setAssistantText] = useState("");
  const [error, setError] = useState<string | null>(null);

  // We keep a ref to the *current* agent bubble id so text tokens append
  // to the right bubble. Without this, every token would render a new bubble.
  const agentBubbleIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // 上传解析结果是纯文本回显（web.md §3.2.2 一次性解析、结果回显）：流式累积进单个
  // user 气泡，不触发任何 /api/chat 轮次；气泡仍进入 messages，随 history 回传。
  const echoBubbleIdRef = useRef<string | null>(null);

  const streamEcho = useCallback((content: string) => {
    const existingId = echoBubbleIdRef.current;
    if (existingId !== null) {
      // 追加而非替换：后端流式逐段返回解析结果（web.md §3.2.2 累积回显）。
      setMessages((prev) =>
        prev.map((m) =>
          m.id === existingId ? { ...m, content: m.content + content } : m,
        ),
      );
      return;
    }
    const id = nextId("user");
    echoBubbleIdRef.current = id;
    setMessages((prev) => [...prev, { id, role: "user", content }]);
  }, []);

  const endEcho = useCallback(() => {
    echoBubbleIdRef.current = null;
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    agentBubbleIdRef.current = null;
    setStatus("idle");
    setMessages([]);
    setToolCards([]);
    setAssistantText("");
    setError(null);
  }, []);

  const send = useCallback(
    async (
      message: string,
      history: ChatMessage[] = [],
      conversationId?: string,
    ): Promise<void> => {
      // Cancel any in-flight stream before starting a new one.
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      // Add user bubble + placeholder agent bubble.
      const userId = nextId("user");
      const agentId = nextId("agent");
      agentBubbleIdRef.current = agentId;

      setMessages((prev) => [
        ...prev,
        { id: userId, role: "user", content: message },
        { id: agentId, role: "agent", content: "" },
      ]);
      setAssistantText("");
      setToolCards([]);
      setError(null);
      setStatus("sending");

      try {
        setStatus("streaming");
        await streamChat(
          {
            message,
            conversationId,
            history: truncateHistory(history),
          },
          (ev: SseEvent) => {
            switch (ev.type) {
              case "text": {
                const content =
                  typeof ev.data.content === "string" ? ev.data.content : "";
                setAssistantText((prev) => prev + content);
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === agentId ? { ...m, content: m.content + content } : m,
                  ),
                );
                break;
              }
              case "tool_call": {
                const name =
                  typeof ev.data.name === "string" ? ev.data.name : "tool";
                setToolCards((prev) => [
                  ...prev,
                  { name, status: "running" },
                ]);
                break;
              }
              case "tool_result": {
                const name =
                  typeof ev.data.name === "string" ? ev.data.name : "tool";
                const result =
                  typeof ev.data.result === "string" ? ev.data.result : "";
                setToolCards((prev) => {
                  // Flip the matching running card to done; otherwise append.
                  const idx = prev.findIndex((c) => c.name === name && c.status === "running");
                  if (idx === -1) {
                    return [...prev, { name, status: "done", result }];
                  }
                  const next = prev.slice();
                  next[idx] = { name, status: "done", result };
                  return next;
                });
                break;
              }
              case "interrupt": {
                // Negligible per web.md §1.3 — backend declares it but never
                // emits it. We receive it just to prove the parser doesn't
                // crash; no UI side effect.
                break;
              }
              case "error": {
                const msg =
                  typeof ev.data.message === "string"
                    ? ev.data.message
                    : "对话出错";
                setError(msg);
                setStatus("error");
                setMessages((prev) => [
                  ...prev,
                  { id: nextId("error"), role: "error", content: msg },
                ]);
                break;
              }
              case "done":
                // Terminal — handled by streamChat's await path.
                break;
              default:
                // Unknown / message — ignore.
                break;
            }
          },
          { signal: controller.signal },
        );
        setStatus("idle");
      } catch (err) {
        if (controller.signal.aborted) {
          // User-triggered cancel — leave messages as-is, return to idle.
          setStatus("idle");
          return;
        }
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg);
        setStatus("error");
        setMessages((prev) => [
          ...prev,
          { id: nextId("error"), role: "error", content: msg },
        ]);
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
        agentBubbleIdRef.current = null;
      }
    },
    [],
  );

  return { status, messages, toolCards, assistantText, error, send, streamEcho, endEcho, reset };
}
