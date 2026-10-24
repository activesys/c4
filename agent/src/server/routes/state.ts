// c4/agent/src/server/routes/state.ts — GET /api/state、POST /api/state/reset
// Returns the current AgentStateSummary for the frontend dashboard.
// Design: agent.md §3.1 — AgentState annotation fields: phase, accessPlan, status.

import { Router, type Request, type Response } from "express";
import type { AgentStateProvider, AgentStateWriter } from "../types.js";

// ── Router Factory ────────────────────────────────────────
/**
 * Create the state router with the agent state provider.
 *
 * @param stateProvider - Provider that reads current agent state
 * @param stateWriter - Optional writer; provided时挂载 POST /reset
 *   （「开启新对话」把全局 phase 复位为 idle，2026-10-05 用户指令）
 * @param displayService - Optional display-session terminator；提供时 reset 级联
 *   终止活跃显示会话（2026-10-24 用户裁定：丢弃当前所有需求的完整闭环，
 *   web.md §3.4.2/§3.7.6）
 * @returns Express Router handling GET /api/state
 */
export function createStateRouter(
    stateProvider: AgentStateProvider,
    stateWriter?: AgentStateWriter,
    displayService?: { hasActiveSession(): boolean; stop(): void },
): Router {
    const router = Router();

    /**
     * GET /api/state
     *
     * Returns AgentStateSummary:
     *   { phase, hasAccessPlan, lastError, siteName }
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

    /**
     * POST /api/state/reset — 开启新对话时复位全局会话状态：
     * phase → idle、accessPlan 撤销；并级联终止活跃显示会话（2026-10-24 用户
     * 裁定：丢弃当前所有需求——显示会话住在 DisplayService、不在 phase 里，
     * 不级联会留下还在跑的显示表格，web.md §3.4.2/§3.7.6）。旧会话草稿按
     * conversationId 隔离留存（LRU 上限内），新会话自然以全新草稿开始。
     */
    router.post("/reset", (_req: Request, res: Response) => {
        if (stateWriter === undefined) {
            res.status(501).json({
                success: false,
                error: "状态重置不可用（缺少状态写入器）",
            });
            return;
        }
        stateWriter.setPhase("idle");
        stateWriter.setAccessPlan(false);
        if (displayService !== undefined && displayService.hasActiveSession()) {
            displayService.stop();
        }
        res.status(200).json({ success: true });
    });

    return router;
}
