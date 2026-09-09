// c4/agent/src/display/session.ts — 点位显示会话管理器
// 设计：agent.md §3.6.3（DisplaySession 模型）/ §3.6.2（状态判定与降级）。
// 单活跃会话：新会话隐式结束旧会话（"切换即取消"）。
// 数据面：每 tick 一次 read_points 批量调用；频率 = seq 变位差分（滑动 60s 窗口）；
// 陈旧阈值自适应 = max(staleThresholdMs, 3×最近一次非空窗口平均间隔，剪空后保留)。
// 会话仅存内存：Agent 重启即中止，不持久化（C4_RS_00131 数据不落地）。

import { readFileSync } from "node:fs";

import type { MultiServerMCPClient } from "@langchain/mcp-adapters";
import { readPoints, type ReadEntry } from "./shm_client.js";

export type DisplayMode = "realtime" | "cumulative";
export type PointState = "ok" | "no_data" | "stale";
export type EndedReason =
    | "completed_count"
    | "completed_duration"
    | "stopped"
    | "replaced"
    | "error";

export interface CumulativeRecord {
    tick: number;
    t: number; // 采集时间戳 ms
    v: number | string;
}

export interface PointSnapshot {
    key: string;
    /** 用户输入的中文点名（display_points displayNames 传入）；缺省时前端回退显示 key */
    name?: string;
    value: number | string | null;
    timestampMs: number | null;
    state: PointState;
    staleForMs?: number;
    freq: { count: number; intervalMs: number };
    lastError?: string;
    // cumulative 模式：since 游标增量（在 getSnapshot 中填充）
    records?: CumulativeRecord[];
}

export interface SessionSnapshot {
    active: true;
    sessionId: string;
    intervalMs: number;
    mode: DisplayMode;
    tick: number;
    terminateRemaining?: string;
    degraded?: boolean;
    points: PointSnapshot[];
}

export interface LastSessionSummary {
    endedReason: EndedReason;
    finalTick: number;
}

export interface DisplayPayload {
    active: boolean;
    session?: SessionSnapshot;
    lastSession?: LastSessionSummary;
}

interface SessionPoint {
    key: string;
    /** 用户输入的中文点名（可选，随会话驻留内存） */
    name?: string;
    shmId: number;
    // 最近一次成功读取
    value: number | string | null;
    valueRaw?: string;
    timestampMs: number | null;
    state: PointState;
    staleForMs?: number;
    lastError?: string;
    // 频率统计
    lastSeq?: number;
    changes: number[]; // 变位 tick 时刻（ms），滑动 60s 窗口
    lastAvgInterval: number | null; // 最近一次非空窗口计算的平均间隔（剪空后保留）
    // 累积缓冲
    records: CumulativeRecord[];
}

const FREQ_WINDOW_MS = 60_000;
const MAX_CUMULATIVE = 600;
const MAX_CONSECUTIVE_FAILURES = 3;

interface ActiveSession {
    id: string;
    points: SessionPoint[];
    mode: DisplayMode;
    intervalMs: number;
    terminate: { kind: "duration"; deadlineMs: number } | { kind: "count"; budget: number } | { kind: "manual" };
    tick: number;
    consecutiveFailures: number;
    degraded: boolean;
    timer: ReturnType<typeof setTimeout> | null;
    ticking: boolean;
}

export interface DisplayServiceOptions {
    multiClient: MultiServerMCPClient;
    /** ~/.local/c4 语义下的 config.json 路径（点位枚举来源） */
    configPath: string;
    staleThresholdMs?: number;
    logger?: { info(msg: string): void; warn(msg: string): void; error(msg: string): void };
}

function nowMs(): number {
    return Date.now();
}

export class DisplayService {
    private readonly multiClient: MultiServerMCPClient;
    private readonly configPath: string;
    private readonly staleThresholdMs: number;
    private readonly logger?: DisplayServiceOptions["logger"];

    private session: ActiveSession | null = null;
    private lastSession: LastSessionSummary | null = null;
    private seqCounter = 0;

    constructor(options: DisplayServiceOptions) {
        this.multiClient = options.multiClient;
        this.configPath = options.configPath;
        this.staleThresholdMs = options.staleThresholdMs ?? 60_000;
        this.logger = options.logger;
    }

    // ── 点位发现（C4_FUN_00085：writer-only 枚举，reader 引用不产生独立条目）──

    listPoints(filter?: string): Array<{
        key: string;
        addr: number;
        shm_id: number;
        instance: string;
    }> {
        let config: Record<string, unknown>;
        try {
            config = JSON.parse(readFileSync(this.configPath, "utf-8")) as Record<string, unknown>;
        } catch {
            return [];
        }
        const shmCfg = config["c4_shm_manager"] as
            | { writer?: string[]; reader?: string[] }
            | undefined;
        if (!shmCfg || !Array.isArray(shmCfg.writer)) {
            return [];
        }

        const entries: Array<{ key: string; addr: number; shm_id: number; instance: string }> = [];
        for (const svcType of shmCfg.writer) {
            const section = config[svcType] as
                | Array<Record<string, unknown>>
                | undefined;
            if (!Array.isArray(section)) {
                continue;
            }
            for (const inst of section) {
                const instanceId = typeof inst["id"] === "string" ? inst["id"] : "";
                const points = Array.isArray(inst["points"]) ? inst["points"] : [];
                for (const pt of points) {
                    const p = pt as { id?: unknown; addr?: unknown; shm_id?: unknown };
                    const key = `${instanceId}.${String(p.id ?? "")}`;
                    entries.push({
                        key,
                        addr: typeof p.addr === "number" ? p.addr : 0,
                        shm_id: typeof p.shm_id === "number" ? p.shm_id : 0,
                        instance: instanceId,
                    });
                }
            }
        }

        if (filter && filter.length > 0) {
            const f = filter.toLowerCase();
            return entries.filter(
                (e) => e.key.toLowerCase().includes(f) || e.instance.toLowerCase().includes(f),
            );
        }
        return entries;
    }

    // ── 会话生命周期（C4_FUN_00084）──

    createSession(options: {
        pointKeys: string[];
        /** pointKey → 用户输入的中文点名（展示用，空串/空白视为未提供） */
        displayNames?: Record<string, string>;
        mode?: DisplayMode;
        intervalMs?: number;
        durationMinutes?: number;
        refreshCount?: number;
    }): SessionSnapshot {
        const intervalMs = options.intervalMs ?? 1000;
        if (intervalMs < 250) {
            throw new Error("INTERVAL_TOO_LOW: intervalMs must be >= 250");
        }
        if (options.pointKeys.length === 0) {
            throw new Error("NO_POINTS: pointKeys is empty");
        }
        if (options.pointKeys.length > 1000) {
            throw new Error("TOO_MANY_POINTS: pointKeys exceeds read_points single-call limit (1000)");
        }

        // key → shm_id 解析（config.json 的 writer 已接入点位）
        const all = this.listPoints();
        const byKey = new Map(all.map((p) => [p.key, p]));
        const points: SessionPoint[] = [];
        const unknown: string[] = [];
        for (const key of options.pointKeys) {
            const hit = byKey.get(key);
            if (hit && hit.shm_id > 0) {
                const displayName = options.displayNames?.[key];
                const name =
                    typeof displayName === "string" && displayName.trim().length > 0
                        ? displayName.trim()
                        : undefined;
                points.push({
                    key,
                    name,
                    shmId: hit.shm_id,
                    value: null,
                    timestampMs: null,
                    state: "no_data",
                    changes: [],
                    lastAvgInterval: null,
                    records: [],
                });
            } else {
                unknown.push(key);
            }
        }
        if (unknown.length > 0) {
            throw new Error(
                `UNKNOWN_POINT_KEY: ${unknown.join(", ")} — 请先调用 list_points 获取可用的 pointKeys`,
            );
        }

        // 切换即取消：隐式结束旧会话
        this.endSession("replaced");

        let terminate: ActiveSession["terminate"] = { kind: "manual" };
        if (options.durationMinutes !== undefined) {
            terminate = {
                kind: "duration",
                deadlineMs: nowMs() + options.durationMinutes * 60_000,
            };
        } else if (options.refreshCount !== undefined) {
            terminate = { kind: "count", budget: options.refreshCount };
        }

        const session: ActiveSession = {
            id: `ds_${Date.now()}_${Math.floor(Math.random() * 1e6)}`,
            points,
            mode: options.mode ?? "realtime",
            intervalMs,
            terminate,
            tick: 0,
            consecutiveFailures: 0,
            degraded: false,
            timer: null,
            ticking: false,
        };
        this.session = session;
        this.lastSession = null;
        this.scheduleTick(session);
        return this.getSnapshot().session as SessionSnapshot;
    }

    stop(pointKeys?: string[]): void {
        const s = this.session;
        if (!s) {
            return;
        }
        if (pointKeys === undefined) {
            this.endSession("stopped");
            return;
        }
        const keys = new Set(pointKeys);
        s.points = s.points.filter((p) => !keys.has(p.key));
        if (s.points.length === 0) {
            this.endSession("stopped");
        }
    }

    /** 是否存在活跃会话（pointKeys 有效性校验用） */
    hasActiveSession(): boolean {
        return this.session !== null;
    }

    private endSession(reason: EndedReason): void {
        const s = this.session;
        if (!s) {
            return;
        }
        if (s.timer) {
            clearTimeout(s.timer);
            s.timer = null;
        }
        this.session = null;
        this.lastSession = { endedReason: reason, finalTick: s.tick };
    }

    private scheduleTick(session: ActiveSession): void {
        if (this.session !== session) {
            return;
        }
        session.timer = setTimeout(() => {
            void this.tickOnce(session);
        }, session.intervalMs);
    }

    private async tickOnce(session: ActiveSession): Promise<void> {
        // 会话已被替换/停止（竞态防护，agent.md §3.6.3 tick 竞态）
        if (this.session !== session) {
            return;
        }
        session.ticking = true;
        try {
            const shmIds = session.points.map((p) => p.shmId);
            let result;
            try {
                result = await readPoints(this.multiClient, shmIds);
            } catch (err) {
                // 调用级失败：本轮整体跳过，保持上次值与状态（不杜撰）
                session.consecutiveFailures += 1;
                if (session.consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
                    session.degraded = true;
                    this.logger?.warn(
                        `点位显示降级：读取通道连续失败 ${session.consecutiveFailures} 轮`,
                    );
                }
                return; // 失败轮次不计入终止条件（C4_RS_00056 终止途径封闭）
            }

            // await 返回后校验会话身份（竞态防护）
            if (this.session !== session) {
                return;
            }

            // 成功：复位降级与失败计数
            const wasDegraded = session.degraded;
            session.consecutiveFailures = 0;
            session.degraded = false;
            if (wasDegraded) {
                this.logger?.info("点位显示：读取通道已恢复");
            }

            session.tick += 1;
            const byId = new Map<number, ReadEntry>();
            for (const r of result.reads) {
                byId.set(r.shm_id, r);
            }
            const errById = new Map<number, string>();
            for (const e of result.errors) {
                errById.set(e.shm_id, e.status);
            }

            const now = nowMs();
            for (const p of session.points) {
                p.lastError = undefined;
                const contention = errById.get(p.shmId);
                if (contention !== undefined) {
                    p.lastError = contention;
                    continue; // 沿用上次成功值与状态
                }
                const r = byId.get(p.shmId);
                if (!r) {
                    p.lastError = "missing_in_response";
                    continue;
                }
                if (r.status === "no_data") {
                    p.state = "no_data";
                    p.value = null;
                    p.timestampMs = null;
                    p.staleForMs = undefined;
                    continue;
                }
                if (r.status !== "ok" || r.timestamp_ms === undefined || r.seq === undefined) {
                    p.lastError = "malformed_read";
                    continue;
                }

                // 频率：seq 变位差分（首个 tick 仅初始化 lastSeq）
                const prev = p.lastSeq;
                if (prev !== undefined && r.seq !== prev) {
                    p.changes.push(now);
                }
                p.lastSeq = r.seq;
                // 滑动 60s 窗口剪枝
                while (p.changes.length > 0 && now - p.changes[0] > FREQ_WINDOW_MS) {
                    p.changes.shift();
                }

                // 自适应陈旧阈值：max(staleThresholdMs, 3×最近一次非空窗口平均间隔)
                if (p.changes.length >= 2) {
                    let sum = 0;
                    for (let i = 1; i < p.changes.length; i++) {
                        sum += p.changes[i] - p.changes[i - 1];
                    }
                    p.lastAvgInterval = sum / (p.changes.length - 1);
                }
                // 剪空后保留 lastAvgInterval（最近一次非空窗口计算值），直至新变位重算
                const adaptiveMs = 3 * (p.lastAvgInterval ?? 0);
                const effectiveThreshold = Math.max(this.staleThresholdMs, adaptiveMs);

                const ageMs = now - Number(r.timestamp_ms ?? 0);
                const displayedValue = this.displayValue(r);
                p.value = displayedValue;
                p.valueRaw = r.value_raw;
                p.timestampMs = r.timestamp_ms;

                if (ageMs > effectiveThreshold) {
                    p.state = "stale";
                    p.staleForMs = ageMs;
                } else {
                    p.state = "ok";
                    p.staleForMs = undefined;
                }

                // 累积缓冲（仅累积模式，上限 600 条）
                if (session.mode === "cumulative") {
                    p.records.push({ tick: session.tick, t: r.timestamp_ms, v: displayedValue });
                    if (p.records.length > MAX_CUMULATIVE) {
                        p.records.shift();
                    }
                }
            }

            // 终止判定
            if (
                session.terminate.kind === "duration" &&
                nowMs() >= session.terminate.deadlineMs
            ) {
                this.endSession("completed_duration");
                return;
            }
            if (session.terminate.kind === "count" && session.tick >= session.terminate.budget) {
                this.endSession("completed_count");
                return;
            }
        } catch (err) {
            this.logger?.error(
                `点位显示 tick 异常: ${err instanceof Error ? err.message : String(err)}`,
            );
        } finally {
            session.ticking = false;
            this.scheduleTick(session);
        }
    }

    /** 展示值：INT64/UINT64 且绝对值 ≥ 2^53 时以 value_raw 字符串为准（JSON 精度限制） */
    private displayValue(r: ReadEntry): number | string {
        if (
            (r.data_type === 7 || r.data_type === 8) &&
            r.value !== undefined &&
            Math.abs(r.value) >= 2 ** 53 &&
            r.value_raw !== undefined
        ) {
            return r.value_raw;
        }
        return r.value ?? 0;
    }

    // ── 快照（/api/display 载荷，agent.md §3.6.5）──

    getSnapshot(sinceTick?: number): DisplayPayload {
        const s = this.session;
        if (!s) {
            const payload: DisplayPayload = { active: false };
            if (this.lastSession) {
                payload.lastSession = this.lastSession;
            }
            return payload;
        }

        // 游标语义：since 仅在当前会话内有意义；无效/跨会话 → 全量
        let since = typeof sinceTick === "number" && Number.isFinite(sinceTick) ? sinceTick : null;
        if (since !== null && (since < 0 || since >= s.tick)) {
            since = null; // 旧游标/越界 → 全量（安全默认）
        }

        const points: PointSnapshot[] = s.points.map((p) => {
            const snap: PointSnapshot = {
                key: p.key,
                value: p.value,
                timestampMs: p.timestampMs,
                state: p.state,
                freq: {
                    count: p.changes.length,
                    intervalMs:
                        p.changes.length >= 2
                            ? (p.changes[p.changes.length - 1] - p.changes[0]) /
                              (p.changes.length - 1)
                            : 0,
                },
            };
            if (p.name !== undefined) {
                snap.name = p.name;
            }
            if (p.state === "stale" && p.staleForMs !== undefined) {
                snap.staleForMs = p.staleForMs;
            }
            if (p.lastError !== undefined) {
                snap.lastError = p.lastError;
            }
            if (s.mode === "cumulative") {
                snap.records =
                    since === null ? [...p.records] : p.records.filter((r) => r.tick > since);
            }
            return snap;
        });

        let terminateRemaining: string | undefined;
        if (s.terminate.kind === "duration") {
            const remainMs = Math.max(0, s.terminate.deadlineMs - nowMs());
            terminateRemaining = `${Math.ceil(remainMs / 1000)}s`;
        } else if (s.terminate.kind === "count") {
            terminateRemaining = `${s.tick}/${s.terminate.budget}`;
        }

        const snapshot: SessionSnapshot = {
            active: true,
            sessionId: s.id,
            intervalMs: s.intervalMs,
            mode: s.mode,
            tick: s.tick,
            points,
        };
        if (terminateRemaining !== undefined) {
            snapshot.terminateRemaining = terminateRemaining;
        }
        if (s.degraded) {
            snapshot.degraded = true;
        }
        return { active: true, session: snapshot };
    }

    activeSessionId(): string | null {
        return this.session?.id ?? null;
    }
}
