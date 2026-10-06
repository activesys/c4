// c4/agent/src/server/types.ts — Server-side agent interface
// Defines the contract between Express routes and the Agent instance.
// The actual implementation is orchestrator/orchestrator.ts (Workflow 编排器).

import type { AgentPhase } from "../types/index.js";
import type { SiteInfo } from "../site_config.js";

// ── Agent Invoke Input ────────────────────────────────────
export interface AgentInvokeInput {
  messages: Array<{ role: string; content: string }>;
  /** 会话 ID（用于运行日志关联；由 chat 路由生成并传入） */
  conversationId?: string;
  /**
   * 取消信号（客户端断开/SSE 中止时触发）：编排器透传给在途 LLM 调用——
   * 立即中断而非跑完，dying 回合不再产生后续事件与 phase 写入
   * （2026-10-05：开新对话后徽标被旧回合改回「收集信息中」的根因）
   */
  signal?: AbortSignal;
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

  /** 场站重绑定（2026-10-06 用户指令：场站修改后后续工作立即生效，无需重启）：
   *  Web 顶栏改名（POST /api/site）成功后由 index.ts 回灌运行中编排器——更新
   *  绑定基准与全部活会话草稿，归属判定与新会话默认值立即使用新场站。 */
  rebindSite(site: SiteInfo): void;
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
