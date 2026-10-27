// c4/agent/frontend/src/components/AgentSwitcher.tsx
// 对话目标 Agent 指示器 — web.md §3.7.1~3.7.3（C4_RS_00048 / C4_FUN_00091）。
// 输入框正下方居中的胶囊控件：当前目标（点色：接入蓝 / 展示绿）+ 下拉箭头；
// 点击（或输入框键入 @，由 ChatView 驱动 atOpen）弹出选择列表（两项），
// ↑↓/Enter/点击确认，Esc/失焦关闭。Tab 循环切换由 ChatView 的输入框键盘处理
// （§3.7.3，preventDefault 不与焦点移动冲突）。选择即切换目标（@ token 不进入
// message 文本——后端不解析自由文本 @，agent.md §2.0.2）。

import { useEffect, useRef } from "react";
import {
  AGENT_LABELS,
  type AgentTarget,
} from "@frontend/hooks/useAgentTarget";

export interface AgentSwitcherProps {
  target: AgentTarget;
  onSwitch: (next: AgentTarget) => void;
  /** @ 浮层开关（ChatView 输入框键入 @ 时置位；Esc/失焦/选择后复位） */
  atOpen: boolean;
  onAtOpenChange: (open: boolean) => void;
}

const TARGETS: AgentTarget[] = ["access", "display"];

export function AgentSwitcher({
  target,
  onSwitch,
  atOpen,
  onAtOpenChange,
}: AgentSwitcherProps): JSX.Element {
  const rootRef = useRef<HTMLDivElement | null>(null);

  // 浮层键盘导航（↑↓/Enter/Esc）——挂在窗口上、浮层开时生效
  useEffect(() => {
    if (!atOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation(); // 不计入双 ESC 取消手势（浮层自己的 Esc 语义）
        onAtOpenChange(false);
        return;
      }
      const idx = TARGETS.indexOf(target);
      if (e.key === "ArrowDown") {
        e.preventDefault();
        onSwitch(TARGETS[(idx + 1) % TARGETS.length]);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        onSwitch(TARGETS[(idx + TARGETS.length - 1) % TARGETS.length]);
      } else if (e.key === "Enter") {
        e.preventDefault();
        onAtOpenChange(false);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [atOpen, target, onSwitch, onAtOpenChange]);

  // 失焦关闭（点击浮层外）
  useEffect(() => {
    if (!atOpen) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current !== null && !rootRef.current.contains(e.target as Node)) {
        onAtOpenChange(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [atOpen, onAtOpenChange]);

  return (
    <div
      ref={rootRef}
      className={`agent-switcher${atOpen ? " agent-switcher--open" : ""}`}
      data-testid="agent-switcher"
    >
      <button
        type="button"
        className={`agent-switcher__trigger agent-switcher__trigger--${target}`}
        data-testid="agent-switcher-trigger"
        aria-haspopup="listbox"
        aria-expanded={atOpen}
        onClick={() => onAtOpenChange(!atOpen)}
      >
        <span
          className="agent-switcher__dot"
          aria-hidden="true"
          data-testid={`agent-dot-${target}`}
        />
        {AGENT_LABELS[target]}
        <svg
          className="agent-switcher__arrow"
          viewBox="0 0 24 24"
          width="14"
          height="14"
          aria-hidden="true"
        >
          <path
            d="m6 9 6 6 6-6"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
      {atOpen ? (
        <div className="agent-switcher__popup" role="listbox" data-testid="agent-popup">
          {TARGETS.map((t) => (
            <button
              key={t}
              type="button"
              role="option"
              aria-selected={t === target}
              className={`agent-switcher__option${
                t === target ? " agent-switcher__option--active" : ""
              }`}
              data-testid={`agent-option-${t}`}
              onClick={() => {
                onSwitch(t);
                onAtOpenChange(false);
              }}
            >
              <span
                className={`agent-switcher__dot agent-switcher__dot--${t}`}
                aria-hidden="true"
              />
              {AGENT_LABELS[t]}
              {t === target ? (
                <span className="agent-switcher__check" aria-hidden="true">
                  ✓
                </span>
              ) : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
