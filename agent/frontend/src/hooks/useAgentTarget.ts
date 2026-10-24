// c4/agent/frontend/src/hooks/useAgentTarget.ts
// 对话目标 Agent 状态 — web.md §3.7（C4_RS_00048 / C4_FUN_00091）。
// 默认「数据接入」且不记忆上次选择：每次进入页面与「开启新对话」后重置为默认
// （ChatView 随 App 的 chatEpoch 重挂载，本 hook 状态随之复位——防「以为在跟
// 接入说话」的错位静默回归）。切换唯一通道 = @ / Tab 显式手势（排他语义）。

import { useCallback, useState } from "react";

export type AgentTarget = "access" | "display";

export const AGENT_LABELS: Record<AgentTarget, string> = {
  access: "数据接入",
  display: "数据展示",
};

export function useAgentTarget(): {
  /** 当前对话目标（默认 access） */
  target: AgentTarget;
  /** 显式切换目标（@ 选择 / Tab 循环共用）；与 setTarget 等价——语义标注 */
  switchTarget: (next: AgentTarget) => void;
  /** Tab 循环切换（接入 ↔ 展示） */
  cycleTarget: () => void;
} {
  const [target, setTarget] = useState<AgentTarget>("access");
  const switchTarget = useCallback((next: AgentTarget) => {
    setTarget(next);
  }, []);
  const cycleTarget = useCallback(() => {
    setTarget((prev) => (prev === "access" ? "display" : "access"));
  }, []);
  return { target, switchTarget, cycleTarget };
}
