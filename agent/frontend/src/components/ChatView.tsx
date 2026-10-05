// c4/agent/frontend/src/components/ChatView.tsx
// Main chat view — web.md §3.1.
//
// 双形态布局：
//   - 落地形态（landing prop，无任何消息时）：www.deepseek.com 风格 hero 居中
//     （LandingHero），输入框随 hero 停靠在中部；
//   - 对话形态（首条消息/上传之后）：chat.deepseek.com 风格，消息列居中、
//     输入框停靠底部。
// 两种形态共用同一套输入框/testid/发送逻辑（e2e 依赖不变）；首次发送或上传
// 时回调 onStarted 通知 App 切换外壳（显示侧栏）。
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
import { LandingHero } from "./LandingHero";
import { Markdown } from "./Markdown";
import { PointDisplayPanel } from "./PointDisplayPanel";
import { streamUpload, classifyFileType } from "@frontend/api/upload";

export interface ChatViewProps {
  /** true = 外壳处于落地形态（App 在新会话时置位，首条消息后解除） */
  landing?: boolean;
  /** 首次实际交互（发送/上传）时触发 — App 据此切换到对话外壳 */
  onStarted?: () => void;
}

export function ChatView({
  landing = false,
  onStarted = () => {},
}: ChatViewProps): JSX.Element {
  const { status, messages, toolCards, assistantText, send, streamEcho, endEcho, planArmed, getConversationId, setConversationId, setPlanArmed, setAssistantText } =
    useChatStream();
  const [draft, setDraft] = useState("");
  const uploadMessage = "请解析此文件中的设备信息";
  const listRef = useRef<HTMLDivElement | null>(null);

  // 落地形态的 hero 只在真正「无内容」时展示——消息或工具卡片一旦出现即视为
  // 已进入对话形态（上传轮的工具卡片可能先于用户消息到达）。
  const landingEmpty =
    landing && messages.length === 0 && toolCards.length === 0;

  // 回合进行中：输入框与发送按钮共用禁用态
  const streaming = status === "sending" || status === "streaming";

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
    onStarted();
    void send(text, history);
  };

  const handleConfirm = () => {
    onStarted();
    void send(CONFIRM_KEYWORD, history);
  };
  const handleCancel = () => {
    onStarted();
    void send(CANCEL_KEYWORD, history);
  };

  const handleFileUpload = async (file: File) => {
    onStarted();
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
            // 不逐段转发为对话轮次。同步累积 assistantText——上传轮直接产出方案时
            // 确认按钮的句式判定依赖它（否则 button_arm 已到但按钮永不渲染）。
            const content =
              typeof ev.data.content === "string" ? ev.data.content : "";
            streamEcho(content);
            setAssistantText((prev) => prev + content);
          } else if (ev.type === "button_arm") {
            setPlanArmed(true);
          } else if (ev.type === "button_disarm") {
            setPlanArmed(false);
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
    <div
      className={`chat-view${landingEmpty ? " chat-view--landing" : ""}`}
      data-testid="chat-view"
    >
      {landingEmpty ? (
        <LandingHero />
      ) : (
        <>
          <PointDisplayPanel />
          <div
            className="chat-view__messages"
            data-testid="message-list"
            ref={listRef}
          >
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
        </>
      )}

      <div className="chat-view__input">
        <textarea
          data-testid="chat-input"
          aria-label="聊天输入框"
          placeholder="问点什么，开始接入…"
          rows={2}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          disabled={streaming}
        />
        <div className="chat-view__input-row">
          <FileUpload onUpload={(file) => void handleFileUpload(file)} />
          <button
            type="button"
            className="chat-view__send"
            onClick={handleSend}
            disabled={streaming || !draft.trim()}
            aria-label="发送"
          >
            {streaming ? (
              <span className="chat-view__send-ellipsis" aria-hidden="true">
                …
              </span>
            ) : (
              <svg
                className="chat-view__send-icon"
                viewBox="0 0 24 24"
                width="22"
                height="22"
                aria-hidden="true"
              >
                <path
                  d="M12 19V5M5.6 11.4 12 5l6.4 6.4"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            )}
          </button>
        </div>
      </div>

      {landingEmpty && (
        <p className="chat-view__disclaimer">
          AI 生成内容仅供参考，执行操作前请确认
        </p>
      )}
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
      {bubble.role === "agent" ? (
        <Markdown text={bubble.display ?? bubble.content} />
      ) : (
        (bubble.display ?? bubble.content)
      )}
    </div>
  );
}
