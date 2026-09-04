// c4/agent/frontend/src/components/ChatView.tsx
// Main chat view — web.md §3.1.
//
// Renders the message stream, the tool cards, the file-upload widget, the
// text input + send button, and the confirm/cancel buttons (visible only
// when matchConfirmPhrase fires on the accumulated agent text).
//
// History truncation is applied inside useChatStream.send, so by the time
// the POST leaves the browser the body is already bounded to N rounds.

import { useEffect, useMemo, useRef, useState } from "react";
import { useChatStream, type ChatBubble } from "@frontend/hooks/useChatStream";
import {
  CONFIRM_KEYWORD,
  CANCEL_KEYWORD,
  matchConfirmPhrase,
} from "@frontend/hooks/useConfirmDetect";
import { ConfirmButtons } from "./ConfirmButtons";
import { ToolCallCard } from "./ToolCallCard";
import { FileUpload } from "./FileUpload";
import { streamUpload, classifyFileType } from "@frontend/api/upload";

export function ChatView(): JSX.Element {
  const { status, messages, toolCards, assistantText, send, streamEcho, endEcho, planArmed, getConversationId, setConversationId } =
    useChatStream();
  const [draft, setDraft] = useState("");
  const [uploadMessage, setUploadMessage] = useState(
    "请解析此文件中的设备信息",
  );
  const listRef = useRef<HTMLDivElement | null>(null);

  // 新消息/工具卡片/流式内容更新时自动滚动到底部
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, toolCards]);

  // 确认按钮双条件：结构化方案已产出（planArmed，output_access_plan 成功）+ 摘要句式命中。
  // 仅凭句式会让信息收集阶段的普通询问（如「请确认转发地址映射」）过早弹出按钮
  const confirmVisible = useMemo(
    () => planArmed && matchConfirmPhrase(assistantText),
    [planArmed, assistantText],
  );

  // Reconstruct a minimal history from the bubbles so the confirm/cancel
  // round-trip includes the prior turns (web.md §3.1.2 多轮上下文).
  // 后端将 history 原样传给 LangChain，仅接受 user/assistant — 气泡角色 agent 需映射。
  const history = useMemo(
    () =>
      messages
        .filter((m) => m.role !== "error")
        .map((m) => ({
          role: m.role === "agent" ? "assistant" : m.role,
          content: m.content,
        })),
    [messages],
  );

  const handleSend = () => {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    void send(text, history);
  };

  const handleConfirm = () => {
    void send(CONFIRM_KEYWORD, history);
  };
  const handleCancel = () => {
    void send(CANCEL_KEYWORD, history);
  };

  const handleFileUpload = async (file: File) => {
    if (classifyFileType(file.name) === "unsupported") {
      // The widget already shows a warning; we still surface a chat-side
      // note so the user sees what happened in context.
      await send("（文件上传被忽略：暂不支持解析此格式）", history);
      return;
    }
    try {
      const returnedCid = await streamUpload(
        { file, message: uploadMessage, conversationId: getConversationId() },
        (ev) => {
          if (ev.type === "text") {
            // 解析结果纯文本回显（web.md §3.2.2）：累积进单个气泡，随 history 回传，
            // 不逐段转发为对话轮次。
            streamEcho(
              typeof ev.data.content === "string" ? ev.data.content : "",
            );
          } else if (ev.type === "error") {
            const msg =
              typeof ev.data.message === "string" ? ev.data.message : "文件解析失败";
            void send(`（文件解析失败：${msg}）`, history);
          }
        },
      );
      if (returnedCid) setConversationId(returnedCid);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      await send(`（文件上传失败：${msg}）`, history);
    } finally {
      endEcho();
    }
  };

  return (
    <div className="chat-view" data-testid="chat-view">
      <div className="chat-view__messages" data-testid="message-list" ref={listRef}>
        {messages.map((m: ChatBubble) => (
          <Bubble key={m.id} bubble={m} />
        ))}
        {toolCards.map((card, idx) => (
          <div key={`tool-${idx}`} className="chat-view__tool">
            <ToolCallCard
              name={card.name}
              status={card.status}
              result={card.result}
            />
          </div>
        ))}
      </div>

      <ConfirmButtons
        visible={confirmVisible}
        onConfirm={handleConfirm}
        onCancel={handleCancel}
      />

      <div className="chat-view__input">
        <FileUpload onUpload={(file) => void handleFileUpload(file)} />
        <input
          type="text"
          data-testid="chat-input"
          aria-label="聊天输入框"
          placeholder="请输入您的问题或需求…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          disabled={status === "sending" || status === "streaming"}
        />
        <button
          type="button"
          onClick={handleSend}
          disabled={status === "sending" || status === "streaming" || !draft.trim()}
          aria-label="发送"
        >
          {status === "sending" || status === "streaming" ? "发送中…" : "发送"}
        </button>
      </div>
    </div>
  );
}

function Bubble({ bubble }: { bubble: ChatBubble }): JSX.Element {
  const cls =
    bubble.role === "user"
      ? "chat-bubble chat-bubble--user"
      : bubble.role === "error"
        ? "chat-bubble chat-bubble--error"
        : "chat-bubble chat-bubble--agent";
  return (
    <div
      data-testid={
        bubble.role === "user"
          ? "user-bubble"
          : bubble.role === "error"
            ? "error-bubble"
            : "agent-bubble"
      }
      className={cls}
    >
      {bubble.display ?? bubble.content}
    </div>
  );
}
