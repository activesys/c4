// c4/agent/src/server/routes/services.ts — GET /api/services
// Returns the MCP service catalog as a JSON array for the frontend dashboard.
// Design: agent.md §3.5 — dashboard component fetches available services.
//
// 图标（协议无关架构）：注册 JSON 的 icon 为相对注册目录 icons/ 子目录的
// 文件路径；本路由将其解析为对外 URL（servicesPath + /icons/<basename>），
// 并以只读静态方式托管该目录。前端拿到的永远是可直接引用的 URL。

import { Router, type Request, type Response } from "express";
import { basename } from "node:path";
import express from "express";
import { getRegistry } from "../../registry/registry.js";
import type { ServiceAliveState } from "../../mcp/client.js";

// ── Router Factory ────────────────────────────────────────
/**
 * Create the services router.
 *
 * @param options.aliveProvider MCP 存活状态 provider（连接状态推导）；提供时每个
 *   服务条目附带 alive/degraded 字段（c4_architecture.md §3.1.1，供 Web 展示与告警）
 * @param options.iconsDir 注册图标文件目录（注册目录下 icons/ 子目录）；存在时挂载
 *   GET <servicesPath>/icons/<basename> 只读静态托管，并把目录条目中的 icon 相对
 *   路径解析为对外 URL。文件缺失时静态托管返回 404，由前端回退默认徽标。
 * @returns Express Router handling GET /api/services
 */
export function createServicesRouter(options: {
    aliveProvider?: () => ServiceAliveState[];
    iconsDir?: string;
    /** 本路由的挂载路径（与 app.ts 一致，默认 /api/services），用于拼图标对外 URL */
    servicesPath?: string;
} = {}): Router {
    const router = Router();
    const { aliveProvider, iconsDir, servicesPath = "/api/services" } = options;

    // 图标静态托管（只读，仅 icons/ 目录内容；express.static 防路径穿越）。
    // 缓存策略与前端 assets 一致：immutable 长缓存——换图标须换文件名。
    if (iconsDir) {
        router.use(
            "/icons",
            express.static(iconsDir, {
                setHeaders(res) {
                    res.setHeader(
                        "Cache-Control",
                        "public, max-age=31536000, immutable",
                    );
                },
            }),
        );
    }

    /** icon 相对路径 → 对外 URL（取 basename，杜绝相对路径逃逸） */
    function iconUrl(icon: string): string {
        return `${servicesPath}/icons/${encodeURIComponent(basename(icon))}`;
    }

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

        const catalog = registry.getServiceCatalogEntries().map((entry) => ({
            ...entry,
            icon: entry.icon ? iconUrl(entry.icon) : undefined,
        }));

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
