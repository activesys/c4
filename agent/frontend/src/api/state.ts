// c4/agent/frontend/src/api/state.ts
// GET /api/state — web.md §3.4.1
//
// Response:
//   200 { success: true, state: { phase, hasAccessPlan, lastError } }
//   500 { success: false, error: string }
//
// Phase is one of "idle"|"collecting"|"planning"|"confirmed"|"executing".

export type AgentPhase =
  | "idle"
  | "collecting"
  | "planning"
  | "confirmed"
  | "executing";

export interface AgentState {
  phase: AgentPhase;
  hasAccessPlan: boolean;
  lastError: string | null;
}

interface StateResponseOk {
  success: true;
  state: AgentState;
}
interface StateResponseErr {
  success: false;
  error: string;
}
type StateResponse = StateResponseOk | StateResponseErr;

export async function fetchState(): Promise<AgentState> {
  const res = await fetch("/api/state", { method: "GET" });
  const body = (await res.json()) as StateResponse;
  if (!res.ok || body.success === false) {
    const msg =
      "success" in body && body.success === false
        ? body.error
        : `状态查询失败: HTTP ${res.status}`;
    throw new Error(msg);
  }
  return body.state;
}
