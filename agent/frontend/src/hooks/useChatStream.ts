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
import { buttonDisplayLabel } from "@frontend/hooks/useConfirmDetect";

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
  display?: string;
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
  /** 确认按钮武装：output_access_plan 成功后 true，output_device_info 成功（方案过期）后 false */
  planArmed: boolean;
  /** 读取当前会话 ID（供上传流程复用同一会话） */
  getConversationId: () => string;
  /** 设置当前会话 ID（上传流程拿到服务端回传的 ID 后回写） */
  setConversationId: (id: string) => void;
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
  // 方案确认按钮的武装状态（web.md §3.1.3）：仅当 output_access_plan 成功后武装，
  // output_device_info 成功（信息更新、方案过期）即解除——信息收集阶段的普通询问
  // 即使含「请确认」句式也不得弹出确认按钮
  const [planArmed, setPlanArmed] = useState(false);

  // We keep a ref to the *current* agent bubble id so text tokens append
  // to the right bubble. Without this, every token would render a new bubble.
  const agentBubbleIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // 会话 ID 持久化：跨轮复用同一会话，后端才能恢复完整跨轮上下文（含工具证据）
  const conversationIdRef = useRef<string>("");

  // 上传解析结果是纯文本回显（web.md §3.2.2 一次性解析、结果回显）：流式累积进单个
  // agent 气泡（解析结果是 Agent 的输出，须按 agent 样式渲染，不得用 user 样式），
  // 不触发任何 /api/chat 轮次；气泡仍进入 messages，随 history 以 assistant 角色回传。
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
    const id = nextId("agent");
    echoBubbleIdRef.current = id;
    setMessages((prev) => [...prev, { id, role: "agent", content }]);
  }, []);

  const endEcho = useCallback(() => {
    echoBubbleIdRef.current = null;
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    agentBubbleIdRef.current = null;
    conversationIdRef.current = "";
    setStatus("idle");
    setMessages([]);
    setToolCards([]);
    setAssistantText("");
    setError(null);
    setPlanArmed(false);
  }, []);

  // 供上传流程读写会话 ID（上传轮与对话轮须同属一个会话）
  const getConversationId = useCallback(() => conversationIdRef.current, []);
  const setConversationId = useCallback((id: string) => {
    if (id) conversationIdRef.current = id;
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
      // Button envelopes travel raw on the wire (backend gate needs the
      // prefix) but render with their friendly label in the bubble.
      const userId = nextId("user");
      const agentId = nextId("agent");
      agentBubbleIdRef.current = agentId;
      const label = buttonDisplayLabel(message);

      setMessages((prev) => [
        ...prev,
        {
          id: userId,
          role: "user",
          content: message,
          ...(label ? { display: label } : {}),
        },
        { id: agentId, role: "agent", content: "" },
      ]);
      setAssistantText("");
      setToolCards([]);
      setError(null);
      setStatus("sending");

      try {
        setStatus("streaming");
        const effectiveCid = conversationId ?? conversationIdRef.current;
        const returnedCid = await streamChat(
          {
            message,
            conversationId: effectiveCid,
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
                // 方案按钮武装/解除（web.md §3.1.3）
                if (name === "output_access_plan" && /"success":\s*true/.test(result)) {
                  setPlanArmed(true);
                }
                if (name === "output_device_info" && /"success":\s*true/.test(result)) {
                  setPlanArmed(false);
                }
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
        if (returnedCid) conversationIdRef.current = returnedCid;
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

  return { status, messages, toolCards, assistantText, error, send, streamEcho, endEcho, planArmed, getConversationId, setConversationId, reset };
}
