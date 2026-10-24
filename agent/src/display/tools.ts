// c4/agent/src/display/tools.ts — 数据展示 Agent 控制面工具（LLM）
// 设计：agent.md §3.6.4 / §3.6.7 —— list_points / display_points 两工具。
// stop_display 已随双 ESC 取消裁定删除（2026-10-24 用户裁定：不通过用户输入识别
// 取消意图——终止统一走 Web 双 ESC → POST /api/cancel → /api/display/stop，
// agent.md §2.4.2/§3.6.5）。控制面走 LLM；数据面由 session.ts 的确定性 ticker
// 执行，不经 LLM。

import { StructuredTool } from "@langchain/core/tools";
import { z } from "zod";
import type { DisplayService } from "./session.js";

export interface DisplayToolsOptions {
    manager: DisplayService;
}

function jsonResult(value: unknown): string {
    return JSON.stringify(value, null, 2);
}

// ── 相似点提示（agent.md §3.6.7②，纯代码——不进 LLM 上下文）──

/** 归一化：小写 + 去空白（与点 key/点名匹配同一归一化哲学的本地近似） */
function normalize_for_suggest(s: string): string {
    return s.toLowerCase().replace(/\s+/g, "");
}

/** 归一化编辑距离（Levenshtein，O(mn)，候选词通常 ≤40 字符——量级安全） */
function edit_distance(a: string, b: string): number {
    const m = a.length;
    const n = b.length;
    if (m === 0) return n;
    if (n === 0) return m;
    let prev = Array.from({ length: n + 1 }, (_, j) => j);
    for (let i = 1; i <= m; i++) {
        const cur = [i];
        for (let j = 1; j <= n; j++) {
            const cost = a[i - 1] === b[j - 1] ? 0 : 1;
            cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
        }
        prev = cur;
    }
    return prev[n];
}

/** 最长公共子串长度（编辑距离的互补信号——前缀/中缀共享也计相似） */
function lcs_length(a: string, b: string): number {
    if (a.length === 0 || b.length === 0) return 0;
    // 滚动两行，行内只看左/上/左上——标准 LCS 子串（连续）变体
    let prev = new Array<number>(b.length + 1).fill(0);
    let best = 0;
    for (let i = 1; i <= a.length; i++) {
        const cur = new Array<number>(b.length + 1).fill(0);
        for (let j = 1; j <= b.length; j++) {
            if (a[i - 1] === b[j - 1]) {
                cur[j] = prev[j - 1] + 1;
                if (cur[j] > best) best = cur[j];
            }
        }
        prev = cur;
    }
    return best;
}

/**
 * 确定性相似度排序：score = lcs*2 - edit（公共子串加权、编辑距离惩罚）。
 * 导出供单元测试钉行为（top-5 截断在调用侧）。
 */
export function suggest_points(
    query: string,
    points: Array<{ key: string; device: string; addr: number }>,
    topN = 5,
): Array<{ pointKey: string; device: string; addr: number }> {
    const q = normalize_for_suggest(query);
    if (q.length === 0) return [];
    const scored = points
        .map((p) => {
            // 候选语料多形态取最优：设备名 / 设备_点名（key 尾段）/ 裸点名（_ 后段）。
            // 单一长语料（设备+尾段拼接）会让 edit 距离被无关字符稀释——短查询
            // 永远负分被过滤，相似点推荐形同虚设（「桨叶角速度」找不到「桨叶角度」
            // 实测）；裸点名形态承担主要命中，设备名形态兜底设备说法的笔误
            const bare = p.key.includes(".") ? p.key.slice(p.key.indexOf(".") + 1) : p.key;
            const forms = [
                p.device,
                bare,
                bare.includes("_") ? bare.slice(bare.indexOf("_") + 1) : bare,
            ];
            const score = Math.max(
                ...forms.map((f) => {
                    const n = normalize_for_suggest(f);
                    if (n.length === 0) return -1;
                    return lcs_length(q, n) * 2 - edit_distance(q, n);
                }),
            );
            return { p, score };
        })
        .filter((x) => x.score > 0)
        .sort((a, b) => b.score - a.score)
        .slice(0, topN)
        .map((x) => ({ pointKey: x.p.key, device: x.p.device, addr: x.p.addr }));
    return scored;
}

/**
 * 创建数据展示 Agent 控制面工具（恰两个，agent.md §3.6.4）。
 * list_points 的确定性等价面是 GET /api/points；display_points 与 POST /api/display
 * 共用同一 DisplayService 入口。终止不经对话文本（双 ESC），故无 stop_display。
 */
export function createDisplayTools(options: DisplayToolsOptions): StructuredTool[] {
    const { manager } = options;

    class ListPointsTool extends StructuredTool {
        name = "list_points";
        description =
            "列出当前实例已接入的全部数据点位（key/地址/所属设备），支持按设备或点位名" +
            "关键词筛选；suggest=true 时对关键词做相似点推荐（用户说法零命中时取 top-5" +
            "相似候选）。用户想查看点位、说法有歧义、或要求显示某设备数据时，先用本工具" +
            "获取可用的 pointKeys。display_points 需要 pointKeys 参数，其取值必须来自本" +
            "工具返回的 key 字段。";
        schema = z.object({
            filter: z
                .string()
                .optional()
                .describe("筛选关键词：匹配设备名、实例 id 或点位名（含点 key 尾段）"),
            suggest: z
                .boolean()
                .optional()
                .describe("相似点推荐模式：对 filter 做确定性相似度排序返回 top-5 候选（零命中时使用）"),
        });

        async _call(input: { filter?: string; suggest?: boolean }): Promise<string> {
            if (input.suggest === true && input.filter && input.filter.length > 0) {
                const all = manager.listPoints();
                const hits = suggest_points(input.filter, all, 5);
                if (hits.length === 0) {
                    return jsonResult({ suggest: [], hint: "没有找到相似点位" });
                }
                return jsonResult({
                    suggest: hits,
                    hint: "以上是与关键词最相似的已接入点位（按相似度排序），请让用户确认要显示哪个",
                });
            }
            const points = manager.listPoints(input.filter);
            if (points.length === 0) {
                return jsonResult({ points: [], hint: "没有匹配的已接入点位；可用 suggest=true 取相似候选" });
            }
            return jsonResult({
                count: points.length,
                points: points.map((p) => ({
                    pointKey: p.key,
                    addr: p.addr,
                    shm_id: p.shm_id,
                    device: p.device,
                })),
            });
        }
    }

    class DisplayPointsTool extends StructuredTool {
        name = "display_points";
        description =
            "建立点位显示会话（对点核验）。pointKeys 来自 list_points；用户用中文点名时" +
            "（如「显示风速」）把点名原文经 displayNames 以 pointKey→中文名 传入；" +
            "mode 为 realtime（实时值）或 cumulative（累积序列，「刷新N次」类缺省）；" +
            "intervalMs 为刷新周期（毫秒，用户说「每2秒」→ 2000；未说不传——服务端按" +
            "1000）；durationMinutes/refreshCount 为终止条件（「显示30秒」→ 0.5；未说" +
            "都不传——服务端按 30 秒）；「一直显示」→ manual=true。建立新会话自动结束" +
            "当前显示。返回会话摘要——回复必须回显三要素（点数/每 X 秒/持续 Y 秒），" +
            "数值以摘要为准。";
        schema = z.object({
            pointKeys: z.array(z.string()).min(1).describe("要显示的点位 key 列表（来自 list_points）"),
            displayNames: z
                .record(z.string(), z.string())
                .optional()
                .describe("pointKey → 用户输入的点名原文（中文）映射；仅传对话中出现过的点名；批量展开时可省略"),
            mode: z
                .enum(["realtime", "cumulative"])
                .optional()
                .describe("展示方式；给出 refreshCount（刷新 N 次）而不指定时缺省 cumulative（序列），其余缺省 realtime"),
            intervalMs: z
                .number()
                .optional()
                .describe("刷新周期毫秒（用户明说才传；缺省 1000；低于 250 会被拒绝）"),
            durationMinutes: z
                .number()
                .optional()
                .describe("持续显示时长（分钟，可为小数如 0.5=30 秒），到期自动停止"),
            refreshCount: z
                .number()
                .optional()
                .describe("刷新次数上限，到达后自动停止"),
            manual: z
                .boolean()
                .optional()
                .describe("一直显示（无自动停止；用户明说「一直显示」才传 true）"),
        });

        async _call(input: {
            pointKeys: string[];
            displayNames?: Record<string, string>;
            mode?: "realtime" | "cumulative";
            intervalMs?: number;
            durationMinutes?: number;
            refreshCount?: number;
            manual?: boolean;
        }): Promise<string> {
            try {
                const snapshot = manager.createSession({
                    pointKeys: input.pointKeys,
                    displayNames: input.displayNames,
                    // 「刷新 N 次」是序列展示语义：mode 未显式给出时随 refreshCount
                    // 缺省 cumulative（§3.6.7 缺省应用归确定性代码——不依赖 LLM 记规则）
                    mode: input.mode ??
                        (input.refreshCount !== undefined ? "cumulative" : "realtime"),
                    intervalMs: input.intervalMs,
                    durationMinutes: input.durationMinutes,
                    refreshCount: input.refreshCount,
                    manual: input.manual,
                });

                // 三要素回显语料（agent.md §3.6.7⑤）——数值全部取自会话事实
                const intervalSec = snapshot.intervalMs / 1000;
                const terminate =
                    snapshot.terminateRemaining !== undefined
                        ? snapshot.terminateRemaining
                        : "持续显示";
                const defaultNote = input.durationMinutes === undefined &&
                    input.refreshCount === undefined &&
                    input.manual !== true
                    ? "（默认显示 30 秒）"
                    : "";
                return jsonResult({
                    ok: true,
                    sessionId: snapshot.sessionId,
                    mode: snapshot.mode,
                    intervalMs: snapshot.intervalMs,
                    points: snapshot.points.map((p) => p.key),
                    devices: [...new Set(snapshot.points.map((p) => p.device))],
                    terminate,
                    echo: `已显示 ${snapshot.points.length} 个点，每 ${intervalSec} 秒刷新，${terminate}${defaultNote}（随时双击 ESC 可提前停止；想一直显示请说「一直显示」）`,
                    hint: "把 echo 字段原样或稍作润色后告知用户；表格出现在页面顶部",
                });
            } catch (err) {
                const msg = err instanceof Error ? err.message : String(err);
                return jsonResult({
                    error: msg,
                    hint: msg.startsWith("UNKNOWN_POINT_KEY")
                        ? "请先调用 list_points 获取可用的 pointKeys"
                        : msg.startsWith("TOO_MANY_POINTS")
                          ? "请按设备分批或收窄范围"
                          : msg.startsWith("TOO_MANY_CUMULATIVE")
                            ? "累积模式单次至多 100 点，请改用实时值模式"
                            : undefined,
                });
            }
        }
    }

    return [new ListPointsTool(), new DisplayPointsTool()];
}
