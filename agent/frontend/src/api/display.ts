// c4/agent/frontend/src/api/display.ts
// 点位显示数据面 + 双 ESC 结构化取消 — web.md §3.5.3/§3.7.6。
// GET /api/display 页面级常驻轮询（useDisplaySession）；POST /api/cancel 独立于
// 发送通道——提交门控（§3.5.6）期间仍可调用（否则显示会话无终止途径）。

export type DisplayPointState = "ok" | "no_data" | "stale";

export interface DisplayPointSnapshot {
  key: string;
  device: string;
  name?: string;
  value: number | string | null;
  timestampMs: number | null;
  state: DisplayPointState;
  staleForMs?: number;
  freq: { count: number; intervalMs: number };
  lastError?: string;
  records?: Array<{ tick: number; t: number; v: number | string }>;
}

export interface DisplaySessionSnapshot {
  active: true;
  sessionId: string;
  intervalMs: number;
  mode: "realtime" | "cumulative";
  tick: number;
  terminateRemaining?: string;
  degraded?: boolean;
  points: DisplayPointSnapshot[];
}

export interface LastSessionSummary {
  endedReason:
    | "completed_count"
    | "completed_duration"
    | "stopped"
    | "replaced"
    | "error";
  finalTick: number;
}

export interface DisplayPayload {
  active: boolean;
  session?: DisplaySessionSnapshot;
  lastSession?: LastSessionSummary;
}

/** GET /api/display（?since= 累积增量游标；P2 表格用全量快照） */
export async function fetchDisplay(
  since?: number,
  signal?: AbortSignal,
): Promise<DisplayPayload> {
  const qs =
    typeof since === "number" && Number.isFinite(since)
      ? `?since=${since}`
      : "";
  const res = await fetch(`/api/display${qs}`, { signal });
  if (!res.ok) throw new Error(`显示状态读取失败: HTTP ${res.status}`);
  return (await res.json()) as DisplayPayload;
}

export interface ResubscribePayload {
  pointKeys: string[];
  displayNames?: Record<string, string>;
  mode?: "realtime" | "cumulative";
  intervalMs?: number;
  durationMinutes?: number;
  refreshCount?: number;
  manual?: boolean;
}

/** POST /api/display（重新订阅按钮——与 LLM display_points 同一服务入口） */
export async function resubscribeDisplay(
  payload: ResubscribePayload,
): Promise<DisplayPayload> {
  const res = await fetch("/api/display", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { error?: string };
    throw new Error(body.error ?? `重新订阅失败: HTTP ${res.status}`);
  }
  return (await res.json()) as DisplayPayload;
}

/** POST /api/cancel（双 ESC 结构化取消——阻塞态中断，web.md §3.7.6） */
export async function cancelBlocked(): Promise<{
  cancelled: "display" | "turn" | null;
}> {
  const res = await fetch("/api/cancel", { method: "POST" });
  if (!res.ok) throw new Error(`取消失败: HTTP ${res.status}`);
  const body = (await res.json()) as { cancelled?: "display" | "turn" | null };
  return { cancelled: body.cancelled ?? null };
}
