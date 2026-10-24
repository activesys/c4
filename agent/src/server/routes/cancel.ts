// c4/agent/src/server/routes/cancel.ts — POST /api/cancel（双 ESC 结构化取消）
// 设计：web.md §3.7.6 / agent.md §2.4.2——双 ESC 只中断「不能提交新内容」的阻塞态，
// 按当前阻塞类型路由（无 body）：
//   ① 活跃显示会话 → DisplayService 终止（与 POST /api/display/stop 同一入口）；
//   ② 在途回合（SSE 流进行中）→ dying 回合取消链路（AbortSignal——已合并的阶段
//      产物保留，agent.md §2.3）；执行窗口内的中止沿用同一链路，事务标记未清时
//      由启动瀑布 L0 回滚兜底（§3.2.3）；
//   非阻塞态（方案武装/收集停等）→ 空操作（改需求走改口、丢弃全部走
//   POST /api/state/reset）。
// 本路由独立于发送通道——提交门控（web.md §3.5.6）期间仍可调用。

import { Router, type Request, type Response } from "express";

export interface CancelRouterOptions {
    /** 活跃显示会话终止（DisplayService） */
    stopDisplay: () => void;
    /** 是否存在活跃显示会话 */
    hasDisplaySession: () => boolean;
    /** 在途回合中止（AgentGroup.cancelActiveTurn）——返回是否中止了回合 */
    abortActiveTurn: () => boolean;
}

export function createCancelRouter(options: CancelRouterOptions): Router {
    const router = Router();

    router.post("/", (_req: Request, res: Response) => {
        try {
            if (options.hasDisplaySession()) {
                options.stopDisplay();
                res.status(200).json({ success: true, cancelled: "display" });
                return;
            }
            if (options.abortActiveTurn()) {
                res.status(200).json({ success: true, cancelled: "turn" });
                return;
            }
            // 非阻塞态：空操作（方案武装/收集停等——双 ESC 无取消对象）
            res.status(200).json({ success: true, cancelled: null });
        } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            res.status(500).json({ success: false, error: message });
        }
    });

    return router;
}
