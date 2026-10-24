// c4/agent/src/orchestrator/agent_group.ts — Agent 组装配单元（agent.md §2.0）
// 单进程双编排器：内部持有数据接入编排器与数据展示编排器，按请求 `agent` 字段
// 分派（缺省 access——存量脚本/旧前端零改动）；实现 server/types.ts 的 C4Agent
// 接口，Express 路由注入零改动。
//
// 会话分桶（§2.0.1）：conversationId 保持用户级主键，接入侧内部键扩展为
// `access:{conversationId}`——两 Agent 的草稿与历史相互独立，上下文不串。
// 上传固定路由接入编排器（/api/upload 无 agent 字段，路由即职责）。
//
// cancelActiveTurn（§2.4.2 阻塞态中断）：双 ESC → POST /api/cancel 在无活跃显示
// 会话时的在途回合中止通道——包装路由层 AbortSignal 为组内控制器，取消时触发
// dying 回合取消链路（已合并的阶段产物保留，agent.md §2.3）。

import type { SiteInfo } from "../site_config.js";
import type { AgentInvokeInput, AgentStreamEvent, C4Agent } from "../server/types.js";

export type AgentTarget = "access" | "display";

export interface AgentGroupOptions {
    /** 数据接入编排器（createOrchestrator 产物） */
    access: C4Agent;
    /** 数据展示编排器（createDisplayOrchestrator 产物） */
    display: {
        invoke(input: AgentInvokeInput): AsyncGenerator<AgentStreamEvent>;
    };
}

/** 接入侧会话桶键（§2.0.1：`access:{conversationId}`） */
export function access_bucket_key(conversationId: string | undefined): string {
    return conversationId ? `access:${conversationId}` : conversationId ?? "access";
}

/**
 * 创建 Agent 组——对 Express 呈现单一 C4Agent，内部按 agent 字段分派两编排器。
 */
export function createAgentGroup(options: AgentGroupOptions): C4Agent & {
    /** POST /api/cancel 的在途回合中止通道（§2.4.2 阻塞态路由之一）；
     *  返回是否中止了在途回合（无可中止对象 = 非阻塞态，空操作） */
    cancelActiveTurn(): boolean;
} {
    const { access, display } = options;

    // 在途回合控制器登记（组级：实例同时至多一个活跃回合——发送互斥由前端门控
    // 与会话语义保证；此处只做取消桥接）
    const activeTurns = new Set<AbortController>();

    async function* invoke(
        input: AgentInvokeInput,
    ): AsyncGenerator<AgentStreamEvent> {
        // 双侧回合统一登记取消桥接（§2.4.2）：display 回合此前未登记——
        // /api/cancel 在无活跃显示会话时无法中止在途订阅回合（92⑦ 实测竞态）
        const controller = new AbortController();
        if (input.signal) {
            const upstream = input.signal;
            const relay = () => controller.abort();
            if (upstream.aborted) {
                controller.abort();
            } else {
                upstream.addEventListener("abort", relay, { once: true });
            }
        }
        activeTurns.add(controller);
        try {
            if (input.agent === "display") {
                yield* display.invoke({ ...input, signal: controller.signal });
                return;
            }
            // 缺省/显式 access：会话键分桶后进接入编排器（桶内 LRU/草稿隔离如常）
            yield* access.invoke({
                ...input,
                agent: undefined,
                conversationId: access_bucket_key(input.conversationId),
                signal: controller.signal,
            });
        } finally {
            activeTurns.delete(controller);
        }
    }

    return {
        invoke,
        rebindSite(site: SiteInfo): void {
            // 场站归属归接入域（agent.md §2.0.3 职责隔离）；展示 Agent 不持场站
            access.rebindSite(site);
        },
        cancelActiveTurn(): boolean {
            const controller = [...activeTurns][activeTurns.size - 1];
            if (controller === undefined) return false;
            controller.abort();
            activeTurns.delete(controller);
            return true;
        },
    };
}
