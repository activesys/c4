// c4/agent/src/server/routes/state.ts — GET /api/state
// Returns the current AgentStateSummary for the frontend dashboard.
// Design: agent.md §3.1 — AgentState annotation fields: phase, accessPlan, status.

import { Router, type Request, type Response } from "express";
import type { AgentStateProvider } from "../types.js";

// ── Router Factory ────────────────────────────────────────
/**
 * Create the state router with the agent state provider.
 *
 * @param stateProvider - Provider that reads current agent state
 * @returns Express Router handling GET /api/state
 */
export function createStateRouter(stateProvider: AgentStateProvider): Router {
    const router = Router();

    /**
     * GET /api/state
     *
     * Returns AgentStateSummary:
     *   { phase: string, hasAccessPlan: boolean, lastError: string | null }
     *
     * Used by the frontend dashboard to show the current workflow phase
     * and any errors.
     */
    router.get("/", (_req: Request, res: Response) => {
        try {
            const state = stateProvider.getState();
            res.status(200).json({
                success: true,
                state,
            });
        } catch (err: unknown) {
            const message =
                err instanceof Error ? err.message : String(err);
            res.status(500).json({
                success: false,
                error: `无法读取 Agent 状态: ${message}`,
            });
        }
    });

    return router;
}
