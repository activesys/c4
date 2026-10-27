// c4/agent/frontend/src/components/ChatView.tsx
// Main chat view — web.md §3.1/§3.7（双 Agent 交互，2026-10-24 P2）。
//
// 双形态布局：
//   - 落地形态（landing prop，无任何消息时）：www.deepseek.com 风格 hero 居中
//     （LandingHero），输入框随 hero 停靠在中部；
//   - 对话形态（首条消息/上传之后）：chat.deepseek.com 风格，消息列居中、
//     输入框停靠底部。
// 两种形态共用同一套输入框/testid/发送逻辑（e2e 依赖不变）；首次发送或上传
// 时回调 onStarted 通知 App 切换外壳（显示侧栏）。
//
// 双 Agent（agent.md §2.0 / web.md §3.7）：
//   - 输入框正下方居中 AgentSwitcher 指示当前目标（默认数据接入，不记忆）；
//     @ 弹出选择浮层、Tab 循环切换——切换唯一通道 = 显式手势；
//   - 提交门控（web.md §3.5.6）：活跃显示会话期间发送禁用（含 manual），
//     输入框保持可编辑（草稿保留）；上传门控 = 目标接入 且 无活跃显示（交集）；
//   - history 分桶（web.md §3.1.2）：气泡携带 origin，发送时仅回传当前目标的
//     history——两 Agent 上下文在后端互不串（agent.md §2.0.1）；
//   - 点位显示表格（PointDisplayTable）随 useDisplaySession 页面级常驻轮询渲染。

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useChatStream, type ChatBubble } from "@frontend/hooks/useChatStream";
import {
  CONFIRM_KEYWORD,
  matchConfirmPhrase,
} from "@frontend/hooks/useConfirmDetect";
import { useAgentTarget } from "@frontend/hooks/useAgentTarget";
import { useDisplaySession } from "@frontend/hooks/useDisplaySession";
import { AgentSwitcher } from "./AgentSwitcher";
import { ConfirmButtons } from "./ConfirmButtons";
import { FileUpload } from "./FileUpload";
import { LandingHero } from "./LandingHero";
import { Markdown } from "./Markdown";
import { PointDisplayTable } from "./PointDisplayTable";
import { ThinkingBlock } from "./ThinkingBlock";
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
  const {
    status,
    messages,
    toolCards,
    thinkingSec,
    turnsHistory,
    assistantText,
    send,
    streamEcho,
    endEcho,
    planArmed,
    getConversationId,
    setConversationId,
    setPlanArmed,
    setAssistantText,
  } = useChatStream();
  const [draft, setDraft] = useState("");
  const uploadMessage = "请解析此文件中的设备信息";
  const rootRef = useRef<HTMLDivElement | null>(null);

  // 对话目标 Agent（web.md §3.7）：默认数据接入、不记忆（重挂载即重置）；
  // @ 浮层开关由输入框键盘驱动
  const { target, switchTarget, cycleTarget } = useAgentTarget();
  const [atOpen, setAtOpen] = useState(false);

  // 活跃显示会话页面级常驻轮询（门控与表格渲染同源，web.md §3.5.3/§3.5.6）
  const { payload: displayPayload, active: displayActive } = useDisplaySession();

  // 落地形态的 hero 只在真正「无内容」时展示——消息或工具卡片一旦出现即视为
  // 已进入对话形态（上传轮的工具卡片可能先于用户消息到达）。
  const landingEmpty =
    landing && messages.length === 0 && toolCards.length === 0;

  // 回合进行中：输入框与发送按钮共用禁用态
  const streaming = status === "sending" || status === "streaming";

  // 提交门控（web.md §3.5.6）：活跃显示会话期间发送禁用（含 manual——无时长也
  // 禁用直到双击 ESC 停止）；门控跨目标生效（切换目标不解除）；输入框保持可编辑
  const sendGated = streaming || displayActive;
  const uploadGated = streaming || displayActive || target === "display";

  // 滚动容器是 app__main（滚动条贴窗口右缘，web.md §3.1 布局约定）——
  // 新消息/思考步骤/计时变化时钉在底部
  useEffect(() => {
    if (landingEmpty) return;
    const main = rootRef.current?.closest(".app__main");
    if (main) main.scrollTop = main.scrollHeight;
  }, [messages, toolCards, thinkingSec, turnsHistory, landingEmpty]);

  // 轮次切分：每个用户气泡开启一轮，其后的 agent/error 气泡归属该轮
  //（上传回显气泡并入前一轮）。每轮在自己的用户气泡之后渲染各自的思考块——
  // 历史轮用快照、当前轮用实时 toolCards，新思考块出现后历史思考块不消失
  //（2026-10-05 用户指令）。快照按「发送序」对齐：send() 每次产生一个用户气泡。
  const turns: ChatBubble[][] = [];
  for (const m of messages) {
    if (m.role === "user" || turns.length === 0) turns.push([m]);
    else turns[turns.length - 1].push(m);
  }
  let userSeen = 0;
  const turnSnaps = turns.map((turn) =>
    turn[0].role === "user" ? (turnsHistory[userSeen++] ?? null) : null,
  );

  // 确认按钮双条件：结构化方案已产出（planArmed，output_access_plan 成功）+ 摘要句式命中。
  // 仅凭句式会让信息收集阶段的普通询问（如「请确认转发地址映射」）过早弹出按钮
  const confirmVisible = useMemo(
    () => planArmed && matchConfirmPhrase(assistantText),
    [planArmed, assistantText],
  );

  // history 分桶（web.md §3.1.2/§3.7.4）：仅回传当前目标的气泡——两 Agent 的
  // 上下文在后端按 {agent}:{conversationId} 分桶互不串（agent.md §2.0.1）。
  const history = useMemo(
    () =>
      messages
        .filter((m) => m.role !== "error" && (m.origin ?? "access") === target)
        .map((m) => ({
          role: m.role === "agent" ? "assistant" : m.role,
          content: m.content,
        })),
    [messages, target],
  );

  const handleSend = () => {
    const text = draft.trim();
    if (!text || sendGated) return;
    setDraft("");
    setAtOpen(false);
    onStarted();
    void send(text, history, undefined, target);
  };

  const handleConfirm = () => {
    onStarted();
    // 确认按钮只在数据接入目标下消费（web.md §3.7.4——展示 Agent 永不产出按钮）
    void send(CONFIRM_KEYWORD, history, undefined, "access");
  };

  const handleFileUpload = async (file: File) => {
    onStarted();
    if (classifyFileType(file.name) === "unsupported") {
      // The widget already shows a warning; we still surface a chat-side
      // note so the user sees what happened in context.
      await send("（文件上传被忽略：暂不支持解析此格式）", history, undefined, "access");
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
            void send(`（文件解析失败：${msg}）`, history, undefined, "access");
          }
        },
      );
      if (returnedCid) setConversationId(returnedCid);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      await send(`（文件上传失败：${msg}）`, history, undefined, "access");
    } finally {
      endEcho();
    }
  };

  // 输入框（落地/对话两形态共用同一实例化 JSX，testid 与发送逻辑单一来源）；
  // 键盘：Enter 发送 / Tab 循环切换目标（§3.7.3，preventDefault）/
  // @ 打开 Agent 选择浮层（§3.7.2——@ token 不进入 message 文本）
  const inputBox = (
    <div className="chat-view__input">
      <textarea
        data-testid="chat-input"
        aria-label="聊天输入框"
        placeholder={
          target === "display"
            ? "输入设备名或点名，开始对点核验…"
            : "问点什么，开始接入…"
        }
        rows={2}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            handleSend();
            return;
          }
          if (e.key === "Tab") {
            // Tab 循环切换目标（web.md §3.7.3）：不与页面焦点移动冲突——
            // 离开输入框用 Esc/点击
            e.preventDefault();
            cycleTarget();
            return;
          }
          if (e.key === "@") {
            e.preventDefault();
            setAtOpen(true);
          }
        }}
        disabled={streaming}
      />
      <div className="chat-view__input-row">
        <FileUpload
          onUpload={(file) => void handleFileUpload(file)}
          disabled={uploadGated}
          disabledReason={
            target === "display"
              ? "切换到数据接入后可上传点表"
              : displayActive
                ? "展示进行中，双击 ESC 停止显示后可上传"
                : undefined
          }
        />
        {/* 当前 Agent 指示器与上传/发送同行（2026-10-27 用户指令）；无边框 */}
        <AgentSwitcher
          target={target}
          onSwitch={switchTarget}
          atOpen={atOpen}
          onAtOpenChange={setAtOpen}
        />
        <button
          type="button"
          className="chat-view__send"
          onClick={handleSend}
          disabled={sendGated || !draft.trim()}
          aria-label="发送"
          title={displayActive ? "展示进行中，双击 ESC 停止显示后可发送" : undefined}
          data-testid="chat-send"
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
  );

  return (
    <div
      ref={rootRef}
      className={`chat-view${landingEmpty ? " chat-view--landing" : ""}`}
      data-testid="chat-view"
    >
      {landingEmpty ? (
        <>
          <LandingHero />
          {inputBox}
          <p className="chat-view__disclaimer">
            AI 生成内容仅供参考，执行操作前请确认
          </p>
        </>
      ) : (
        <>
          <PointDisplayTable payload={displayPayload} />
          <div className="chat-view__messages" data-testid="message-list">
            {turns.map((turn, ti) => {
              const isLive = ti === turns.length - 1;
              const snap = turnSnaps[ti];
              return (
                <Fragment key={turn[0].id}>
                  <Bubble bubble={turn[0]} />
                  {isLive ? (
                    <ThinkingBlock
                      steps={toolCards}
                      active={streaming}
                      elapsedSec={thinkingSec}
                    />
                  ) : snap ? (
                    <ThinkingBlock
                      steps={snap.steps}
                      active={false}
                      elapsedSec={snap.elapsedSec}
                    />
                  ) : null}
                  {turn.slice(1).map((m) => (
                    <Bubble key={m.id} bubble={m} />
                  ))}
                </Fragment>
              );
            })}
          </div>

          {/* 停靠层：确认按钮 + 输入框钉在滚动容器底部（滚动条贴窗口右缘） */}
          <div className="chat-view__dock">
            <ConfirmButtons visible={confirmVisible} onConfirm={handleConfirm} />
            {inputBox}
            <p className="chat-view__disclaimer">
              AI 生成内容仅供参考，执行操作前请确认
            </p>
          </div>
        </>
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
  const originLabel =
    bubble.role === "user" && bubble.origin !== undefined
      ? bubble.origin === "display"
        ? "→ 数据展示"
        : "→ 数据接入"
      : null;
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
      {originLabel !== null ? (
        <span className="chat-bubble__origin" data-testid="bubble-origin">
          {originLabel}
        </span>
      ) : null}
    </div>
  );
}
