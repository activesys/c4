// c4/agent/src/server/routes/services.ts — GET /api/services
// Returns the MCP service catalog as a JSON array for the frontend dashboard.
// Design: agent.md §3.5 — dashboard component fetches available services.

import { Router, type Request, type Response } from "express";
import { getRegistry } from "../../registry/registry.js";

// ── Router Factory ────────────────────────────────────────
/**
 * Create the services router.
 *
 * @returns Express Router handling GET /api/services
 */
export function createServicesRouter(): Router {
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

        res.status(200).json({
            success: true,
            services: catalog,
            count: catalog.length,
        });
    });

    return router;
}
