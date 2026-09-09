// c4/agent/src/display/tools.ts — 对点核验控制面工具（LLM）
// 设计：agent.md §3.6.4 —— list_points / display_points / stop_display。
// 控制面走 LLM；数据面由 session.ts 的确定性 ticker 执行，不经 LLM。

import { StructuredTool } from "@langchain/core/tools";
import { z } from "zod";
import type { DisplayService } from "./session.js";

export interface DisplayToolsOptions {
    manager: DisplayService;
}

function jsonResult(value: unknown): string {
    return JSON.stringify(value, null, 2);
}

/**
 * 创建对点核验控制面三工具。
 * list_points 的确定性等价面是 GET /api/points；display_points/stop_display 是
 * POST /api/display 与 POST /api/display/stop——同一 DisplayService 入口。
 */
export function createDisplayTools(options: DisplayToolsOptions): StructuredTool[] {
    const { manager } = options;

    class ListPointsTool extends StructuredTool {
        name = "list_points";
        description =
            "列出当前实例已接入的全部数据点位（key/地址/shm_id/所属设备），" +
            "支持按设备或点位名关键词筛选。用户想查看点位、对点名有歧义、" +
            "或要求显示某设备数据时，先用本工具获取可用的 pointKeys。" +
            "display_points 需要 pointKeys 参数，其取值必须来自本工具返回的 key 字段。";
        schema = z.object({
            filter: z
                .string()
                .optional()
                .describe("可选筛选关键词：匹配设备名（实例 id）或点位名"),
        });

        async _call(input: { filter?: string }): Promise<string> {
            const points = manager.listPoints(input.filter);
            if (points.length === 0) {
                return jsonResult({ points: [], hint: "没有匹配的已接入点位" });
            }
            return jsonResult({
                count: points.length,
                points: points.map((p) => ({
                    pointKey: p.key,
                    addr: p.addr,
                    shm_id: p.shm_id,
                    device: p.instance,
                })),
            });
        }
    }

    class DisplayPointsTool extends StructuredTool {
        name = "display_points";
        description =
            "建立点位持续显示会话（对点核验）。pointKeys 来自 list_points；" +
            "用户用中文点名时（如「显示风速」），把用户输入的点名原文通过 displayNames " +
            "以 pointKey→中文名 传入，展示卡片将以中文名为主、key 为辅；" +
            "mode 为 realtime（实时值，缺省）或 cumulative（累积序列）；" +
            "durationMinutes/refreshCount 为可选终止条件（如持续 5 分钟 / 刷新 20 次）。" +
            "建立新会话会自动取消当前显示（切换即取消）。返回会话摘要，" +
            "展示卡片出现在对话顶部，由前端自动轮询呈现。";
        schema = z.object({
            pointKeys: z.array(z.string()).min(1).describe("要显示的点位 key 列表（来自 list_points）"),
            displayNames: z
                .record(z.string(), z.string())
                .optional()
                .describe("pointKey → 用户输入的点名原文（中文）映射；仅传对话中出现过的点名"),
            mode: z
                .enum(["realtime", "cumulative"])
                .optional()
                .describe("展示方式，缺省 realtime"),
            durationMinutes: z
                .number()
                .optional()
                .describe("持续显示时长（分钟），到期自动停止"),
            refreshCount: z
                .number()
                .optional()
                .describe("刷新次数上限，到达后自动停止"),
        });

        async _call(input: {
            pointKeys: string[];
            displayNames?: Record<string, string>;
            mode?: "realtime" | "cumulative";
            durationMinutes?: number;
            refreshCount?: number;
        }): Promise<string> {
            try {
                const snapshot = manager.createSession({
                    pointKeys: input.pointKeys,
                    displayNames: input.displayNames,
                    mode: input.mode,
                    durationMinutes: input.durationMinutes,
                    refreshCount: input.refreshCount,
                });

                const terminate =
                    input.durationMinutes !== undefined
                        ? `持续 ${input.durationMinutes} 分钟后自动停止`
                        : input.refreshCount !== undefined
                          ? `刷新 ${input.refreshCount} 次后自动停止`
                          : "持续显示，直到用户要求停止或切换点位";

                return jsonResult({
                    ok: true,
                    sessionId: snapshot.sessionId,
                    mode: snapshot.mode,
                    points: snapshot.points.map((p) => p.key),
                    terminate: terminate,
                    hint: "显示卡片已出现在对话顶部；用户说「停止刷新」时调用 stop_display",
                });
            } catch (err) {
                const msg = err instanceof Error ? err.message : String(err);
                return jsonResult({
                    error: msg,
                    hint: msg.startsWith("UNKNOWN_POINT_KEY")
                        ? "请先调用 list_points 获取可用的 pointKeys"
                        : undefined,
                });
            }
        }
    }

    class StopDisplayTool extends StructuredTool {
        name = "stop_display";
        description =
            "停止点位持续显示。不带参数停止整个显示会话；带 pointKeys 仅停止指定点位" +
            "（其余点继续显示）。";
        schema = z.object({
            pointKeys: z
                .array(z.string())
                .optional()
                .describe("要停止的点位 key 列表；缺省停止全部"),
        });

        async _call(input: { pointKeys?: string[] }): Promise<string> {
            manager.stop(input.pointKeys);
            return jsonResult({ ok: true, stopped: input.pointKeys ?? "all" });
        }
    }

    return [new ListPointsTool(), new DisplayPointsTool(), new StopDisplayTool()];
}
