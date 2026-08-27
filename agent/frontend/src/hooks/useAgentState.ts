// c4/agent/frontend/src/hooks/useAgentState.ts
// GET /api/state polling — web.md §3.4.
//
// Polls on a configurable interval and exposes the current AgentState. The
// caller can also force a refresh (e.g. after a stream finishes) to minimize
// the polling-lag artifact noted in §3.4.2.

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchState, type AgentState } from "@frontend/api/state";

export interface UseAgentStateReturn {
  phase: AgentState["phase"] | "unknown";
  hasAccessPlan: boolean;
  lastError: string | null;
  refresh: () => Promise<void>;
}

export function useAgentState(intervalMs = 1000): UseAgentStateReturn {
  const [state, setState] = useState<AgentState | null>(null);
  const mountedRef = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchState();
      if (mountedRef.current) setState(next);
    } catch (err) {
      // Swallow polling errors — the next tick will retry.
      if (err instanceof Error && err.message) {
        // eslint-disable-next-line no-console
        console.warn("[useAgentState] poll failed:", err.message);
      }
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void refresh();
    const id = window.setInterval(() => void refresh(), intervalMs);
    return () => {
      mountedRef.current = false;
      window.clearInterval(id);
    };
  }, [refresh, intervalMs]);

  return {
    phase: state?.phase ?? "unknown",
    hasAccessPlan: state?.hasAccessPlan ?? false,
    lastError: state?.lastError ?? null,
    refresh,
  };
}
