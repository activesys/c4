// c4/agent/frontend/src/components/ThinkingBlock.tsx
// 思考过程折叠块 — 参考 chat.deepseek.com 的「已思考（用时 N 秒）∨」。
//
// C4 是确定性流水线，没有模型 reasoning 文本；这里展示的「思考过程」是本轮
// 真实执行的工具调用流水线（意图提取/注册表查询/方案装配/执行步骤）。
// 进行中标题「思考中…（已用时 N 秒）」实时追加步骤；结束后「已思考（用时 N 秒）」。
// 默认展开（2026-10-05 用户指令），用户手动折叠后保持折叠；无步骤且已结束的
// 回合不渲染。

import { useState } from "react";
import type { ToolCardState } from "@frontend/hooks/useChatStream";
import { ToolCallCard } from "./ToolCallCard";

export interface ThinkingBlockProps {
  steps: ToolCardState[];
  /** 回合是否仍在进行（sending/streaming） */
  active: boolean;
  /** 已用时秒数（active 时由父组件每 0.5s 刷新） */
  elapsedSec: number;
}

export function ThinkingBlock({
  steps,
  active,
  elapsedSec,
}: ThinkingBlockProps): JSX.Element | null {
  // 默认展开；是否折叠完全交给用户手动切换（回合结束不自动收起）
  const [open, setOpen] = useState(true);

  if (!active && steps.length === 0) {
    return null;
  }

  return (
    <div
      className="thinking"
      data-testid="thinking-block"
      data-active={active ? "true" : "false"}
    >
      <button
        type="button"
        className="thinking__header"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <svg
          className={`thinking__icon${active ? " thinking__icon--active" : ""}`}
          viewBox="0 0 24 24"
          width="16"
          height="16"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M12 3v3m0 12v3M5.6 5.6l2.2 2.2m8.4 8.4 2.2 2.2M3 12h3m12 0h3M5.6 18.4l2.2-2.2m8.4-8.4 2.2-2.2" />
        </svg>
        <span className="thinking__title">
          {active ? `思考中…（已用时 ${elapsedSec} 秒）` : `已思考（用时 ${elapsedSec} 秒）`}
        </span>
        <span className="thinking__chevron" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div className="thinking__body">
          {steps.map((s, i) => (
            <ToolCallCard
              key={`${s.name}-${i}`}
              name={s.name}
              status={s.status}
              result={s.result}
            />
          ))}
        </div>
      )}
    </div>
  );
}
