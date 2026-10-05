// c4/agent/src/server/types.ts — Server-side agent interface
// Defines the contract between Express routes and the Agent instance.
// The actual implementation is orchestrator/orchestrator.ts (Workflow 编排器).

import type { AgentPhase } from "../types/index.js";

// ── Agent Invoke Input ────────────────────────────────────
export interface AgentInvokeInput {
  messages: Array<{ role: string; content: string }>;
  /** 会话 ID（用于运行日志关联；由 chat 路由生成并传入） */
  conversationId?: string;
}

// ── Agent Stream Events ───────────────────────────────────
export type AgentStreamEvent =
  | { type: "text"; content: string }
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; name: string; result: string }
  | { type: "button_arm" }
  | { type: "button_disarm"; reason: string }
  | { type: "interrupt"; message: string; interruptId: string }
  | { type: "done" }
  | { type: "error"; message: string };

// ── C4Agent Interface ─────────────────────────────────────
/**
 * Minimal contract for the C4 Agent instance used by Express routes.
 *
 * The actual implementation (createOrchestrator, 缺口驱动九阶段流水线) is assembled
 * in orchestrator/orchestrator.ts and injected into the server at startup.
 */
export interface C4Agent {
  /** Invoke the agent with messages, yielding a stream of events. */
  invoke(input: AgentInvokeInput): AsyncGenerator<AgentStreamEvent>;
}

// ── Agent State (for GET /api/state) ──────────────────────
export interface AgentStateSummary {
  phase: AgentPhase;
  hasAccessPlan: boolean;
  lastError: string | null;
  /** 当前绑定场站名（agent.json 权威配置，§3.2.1.3a；未绑定为 null） */
  siteName: string | null;
}

// ── Agent State Provider ──────────────────────────────────
export interface AgentStateProvider {
  /** Return a summary of the current agent state for the UI dashboard. */
  getState(): AgentStateSummary;
}

// ── Agent State Writer ────────────────────────────────────
/** SuperWorker 在接入流程各阶段写入状态的接口（agent.md §3.2.1.7）。 */
export interface AgentStateWriter {
  setPhase(phase: AgentPhase): void;
  setAccessPlan(exists: boolean): void;
  setError(error: string | null): void;
  /** 场站绑定更新（顶栏中央展示用，2026-10-05 用户指令） */
  setSiteName(name: string | null): void;
}
