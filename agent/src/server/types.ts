// c4/agent/src/server/types.ts — Server-side agent interface
// Defines the contract between Express routes and the Agent instance.
// The actual SuperWorker implementation lives in super_worker/super_worker.ts.

// ── Agent Invoke Input ────────────────────────────────────
export interface AgentInvokeInput {
  messages: Array<{ role: string; content: string }>;
}

// ── Agent Stream Events ───────────────────────────────────
export type AgentStreamEvent =
  | { type: "text"; content: string }
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; name: string; result: string }
  | { type: "interrupt"; message: string; interruptId: string }
  | { type: "done" }
  | { type: "error"; message: string };

// ── C4Agent Interface ─────────────────────────────────────
/**
 * Minimal contract for the C4 Agent instance used by Express routes.
 *
 * The actual implementation (createDeepAgent from deepagents) is assembled
 * in super_worker/super_worker.ts and injected into the server at startup.
 */
export interface C4Agent {
  /** Invoke the agent with messages, yielding a stream of events. */
  invoke(input: AgentInvokeInput): AsyncGenerator<AgentStreamEvent>;
}

// ── Agent State (for GET /api/state) ──────────────────────
export interface AgentStateSummary {
  phase: string;
  hasAccessPlan: boolean;
  lastError: string | null;
}

// ── Agent State Provider ──────────────────────────────────
export interface AgentStateProvider {
  /** Return a summary of the current agent state for the UI dashboard. */
  getState(): AgentStateSummary;
}
