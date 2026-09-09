// c4/agent/src/display/routes.ts — 对点核验数据面 REST
// 设计：agent.md §3.6.5 —— GET /api/points、GET/POST /api/display、POST /api/display/stop。
// 数据面走确定性入口：POST /api/display 与 LLM display_points 工具共用同一 DisplayService。

import { Router, type Request, type Response } from "express";
import type { DisplayService } from "./session.js";

export interface DisplayRouterOptions {
    manager: DisplayService;
}

export function createDisplayRouter(options: DisplayRouterOptions): Router {
    const router = Router();
    const { manager } = options;

    // ── 点位发现（C4_FUN_00085）──
    router.get("/points", (req: Request, res: Response) => {
        const filter = typeof req.query.filter === "string" ? req.query.filter : undefined;
        const points = manager.listPoints(filter);
        res.json({ count: points.length, points });
    });

    // ── 活跃会话状态（前端按 intervalMs 轮询；累积模式 ?since=<tick> 增量）──
    router.get("/display", (req: Request, res: Response) => {
        const sinceRaw = req.query.since;
        const since =
            typeof sinceRaw === "string" && sinceRaw !== "" ? Number(sinceRaw) : undefined;
        res.json(manager.getSnapshot(Number.isFinite(since) ? since : undefined));
    });

    // ── 创建显示会话（重新订阅按钮与 LLM display_points 共用入口）──
    router.post("/display", (req: Request, res: Response) => {
        const body = (req.body ?? {}) as {
            pointKeys?: unknown;
            displayNames?: unknown;
            mode?: unknown;
            intervalMs?: unknown;
            durationMinutes?: unknown;
            refreshCount?: unknown;
        };
        const displayNames: Record<string, string> | undefined =
            body.displayNames !== undefined && typeof body.displayNames === "object"
                ? Object.fromEntries(
                      Object.entries(body.displayNames as Record<string, unknown>)
                          .filter(
                              (e): e is [string, string] =>
                                  typeof e[1] === "string" && e[1].trim().length > 0,
                          )
                          .map(([k, v]) => [k, v.trim()]),
                  )
                : undefined;
        try {
            const snapshot = manager.createSession({
                pointKeys: (body.pointKeys as string[] | undefined) ?? [],
                displayNames,
                mode: body.mode === "cumulative" ? "cumulative" : "realtime",
                intervalMs: typeof body.intervalMs === "number" ? body.intervalMs : undefined,
                durationMinutes:
                    typeof body.durationMinutes === "number" ? body.durationMinutes : undefined,
                refreshCount: typeof body.refreshCount === "number" ? body.refreshCount : undefined,
            });
            res.json({ active: true, session: snapshot });
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            res.status(400).json({ error: msg });
        }
    });

    // ── 停止（整会话或指定点）──
    router.post("/display/stop", (req: Request, res: Response) => {
        const body = (req.body ?? {}) as { pointKeys?: unknown };
        const pointKeys = Array.isArray(body.pointKeys)
            ? (body.pointKeys as string[])
            : undefined;
        manager.stop(pointKeys);
        res.json(manager.getSnapshot());
    });

    return router;
}
