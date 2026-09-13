// c4/agent/src/server/routes/services.ts — GET /api/services
// Returns the MCP service catalog as a JSON array for the frontend dashboard.
// Design: agent.md §3.5 — dashboard component fetches available services.

import { Router, type Request, type Response } from "express";
import { getRegistry } from "../../registry/registry.js";
import type { ServiceAliveState } from "../../mcp/client.js";

// ── Router Factory ────────────────────────────────────────
/**
 * Create the services router.
 *
 * @param aliveProvider MCP 存活状态 provider（连接状态推导）；提供时每个服务条目
 *   附带 alive/degraded 字段（c4_architecture.md §3.1.1，供 Web 展示与告警）
 * @returns Express Router handling GET /api/services
 */
export function createServicesRouter(
    aliveProvider?: () => ServiceAliveState[],
): Router {
    const router = Router();

    /**
     * GET /api/services
     *
     * Returns the L1 service catalog as a JSON array.
     * Each entry: { service_type, display_name, role, protocols[] }
     *
     * Used by the frontend dashboard to display available MCP services
     * and their capabilities.
     */
    router.get("/", (_req: Request, res: Response) => {
        const registry = getRegistry();

        if (!registry.isLoaded) {
            res.status(503).json({
                success: false,
                error: "MCP Service Registry 尚未加载。请等待 Agent 启动完成。",
            });
            return;
        }

        const catalog = registry.getServiceCatalogEntries();

        if (aliveProvider) {
            const alive = new Map(
                aliveProvider().map((a) => [a.service_type, a]),
            );
            const withAlive = catalog.map((entry) => {
                const st = alive.get(entry.service_type);
                return st
                    ? { ...entry, alive: st.alive, degraded: st.degraded }
                    : entry;
            });
            res.status(200).json({
                success: true,
                services: withAlive,
                count: withAlive.length,
            });
            return;
        }

        res.status(200).json({
            success: true,
            services: catalog,
            count: catalog.length,
        });
    });

    return router;
}
