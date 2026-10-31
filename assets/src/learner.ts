// StageLearner 实现——M1 样板闭环：协议别名（阶段 2/5，c4_asset_learning.md
// §3.3 / §11.1）。闭环 = 命中（快路叠加命中记录）→ 确认（handle_confirm 终局
// 采集）→ confirm_streak / usage_count 递增 → 达标沉淀 active → 注入/快路叠加。
//
// 机制锚点（§4.1 逐轮快照 + 终局比对）：用户的改口发生在提取回合合并期，到
// handle_confirm 时草稿已含全部修正——因此各回合的提取提议由 recordProposal
// 记入会话级缓冲，确认成功时以「任意一轮提议 ≠ 最终值」判 corrected；草稿
// LRU 淘汰/重启丢失的确认不产生学习事件（§4.1 显式取舍）。
//
// 沉淀门控（§9.8，2026-10-29 用户裁定）：全局层 usage_count≥5；origin_site 仅
// 溯源不计数；仅 active 可导出。learning 失败不影响主流程——构造失败降级为
// no-op 实例（§1.2 不变量 1：永不静默出错的前提是接入流程本身不被拖垮）。

import { homedir } from "node:os";
import { join } from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { open_assets_db } from "./db.js";
import { alias_key_norm } from "./alias_key_norm.js";

export const GLOBAL_USAGE_THRESHOLD = 5;
export const SITE_STREAK_THRESHOLD = 3;
const MAX_ALIAS_CANDIDATES = 3;
const EVENT_PAYLOAD_LIMIT = 64 * 1024;

export interface LearnedProtocolAlias {
    alias: string;
    canonical: string;
    status: string;
    usage_count: number;
    confirm_streak: number;
}

interface ProposalRecord {
    stage: string;
    value: string;
    at: string;
}

interface HitRecord {
    alias: string;
    canonical: string;
    at: string;
}

interface AssetRow {
    id: number;
    key: string;
    value: string;
    status: string;
    usage_count: number;
    confirm_streak: number;
}

export class AssetLearner {
    private db: DatabaseSync | null = null;
    private proposals = new Map<string, ProposalRecord[]>();
    private learnedHits = new Map<string, HitRecord[]>();
    private aliasCache: LearnedProtocolAlias[] | null = null;

    constructor(readonly dbPath: string) {
        try {
            this.db = open_assets_db(dbPath);
        } catch (err) {
            // 打不开库 → 降级 no-op（学习停摆，接入主流程不受影响）
            console.error(
                `[assets] 资产库打开失败，学习功能降级停摆: ${dbPath}`,
                err instanceof Error ? err.message : err,
            );
            this.db = null;
        }
    }

    private get ok(): boolean {
        return this.db !== null;
    }

    private invalidate(): void {
        this.aliasCache = null;
    }

    // ── 查询（运行期注入 / 快路叠加）─────────────────────────

    /** 全局层协议别名列表（candidate/probation/active；按 usage_count 降权排序）。 */
    learnedProtocolAliases(): LearnedProtocolAlias[] {
        if (!this.ok) return [];
        if (this.aliasCache) return this.aliasCache;
        const rows = this.db!.prepare(
            "SELECT key, value, status, usage_count, confirm_streak FROM assets " +
                "WHERE kind = 'protocol_alias' AND scope = 'global' " +
                "AND status IN ('candidate','probation','active') " +
                "ORDER BY usage_count DESC, id ASC",
        ).all() as Array<{
            key: string;
            value: string;
            status: string;
            usage_count: number;
            confirm_streak: number;
        }>;
        const list: LearnedProtocolAlias[] = [];
        for (const r of rows) {
            try {
                const v = JSON.parse(r.value) as { alias?: unknown; canonical?: unknown };
                if (typeof v.alias !== "string" || typeof v.canonical !== "string") continue;
                list.push({
                    alias: v.alias,
                    canonical: v.canonical,
                    status: r.status,
                    usage_count: r.usage_count,
                    confirm_streak: r.confirm_streak,
                });
            } catch {
                // 值损坏的行跳过（不阻塞其余资产）
            }
        }
        this.aliasCache = list;
        return list;
    }

    // ── 运行期记录（§4.1）────────────────────────────────────

    /** 协议阶段每回合提取提议快照（逐轮；同一会话跨回合累积）。 */
    recordProposal(sessionId: string, stage: string, value: string): void {
        if (!this.ok || !value) return;
        const arr = this.proposals.get(sessionId) ?? [];
        arr.push({ stage, value, at: new Date().toISOString() });
        this.proposals.set(sessionId, arr);
    }

    /** 学习别名快路命中（alias_match_protocol 经学习别名绑定协议时记录）。 */
    recordLearnedHit(sessionId: string, alias: string, canonical: string): void {
        if (!this.ok) return;
        const arr = this.learnedHits.get(sessionId) ?? [];
        arr.push({ alias, canonical, at: new Date().toISOString() });
        this.learnedHits.set(sessionId, arr);
    }

    /**
     * 纠错通道别名学习（§4.4 失败归因：协议误判 → 协议别名资产）。从更正子句
     * 提取别名候选（≤MAX_ALIAS_CANDIDATES 个），排除既有 registry/学习别名与
     * canonical 本身；已存在的键不覆盖（§7.3 冲突不静默）。
     * 返回新建别名资产数。
     */
    learnAliasFromCorrection(
        sessionId: string,
        stage: string,
        toCanonical: string,
        clause: string,
        excludeAliases: ReadonlyArray<string>,
    ): number {
        if (!this.ok) return 0;
        const now = new Date().toISOString();
        const exclude = new Set<string>(excludeAliases.map((a) => alias_key_norm(a)));
        exclude.add(alias_key_norm(toCanonical));
        const tokens = clause
            .split(/[^0-9A-Za-z\u4e00-\u9fff]+/)
            .map((t) => t.trim())
            .filter((t) => t.length >= 2 && t.length <= 12);
        let created = 0;
        for (const t of tokens) {
            if (created >= MAX_ALIAS_CANDIDATES) break;
            const key = alias_key_norm(t);
            if (key === "" || exclude.has(key)) continue;
            const dup = this.db!.prepare(
                "SELECT id FROM assets WHERE kind='protocol_alias' AND scope='global' AND key=?",
            ).get(key);
            if (dup) continue;
            this.db!
                .prepare(
                    "INSERT INTO assets (kind, stage, scope, key, value, status, created_at, updated_at) " +
                        "VALUES ('protocol_alias', ?, 'global', ?, ?, 'candidate', ?, ?)",
                )
                .run(stage, key, JSON.stringify({ alias: t, canonical: toCanonical }), now, now);
            this.insertEvent({
                stage,
                sessionId,
                kind: "protocol_alias",
                assetKey: key,
                proposed: "(新别名候选)",
                final: JSON.stringify({ alias: t, canonical: toCanonical }),
                corrected: true,
                inputRef: `clause:${clause.slice(0, 200)}`,
                at: now,
            });
            created++;
        }
        if (created > 0) this.invalidate();
        return created;
    }

    // ── 终局采集（§4.1 handle_confirm 成功路径、草稿清空之前调用）───

    /**
     * 对 recv(阶段2) / fwd(阶段5) 两侧做终局比对：任意一轮提议 ≠ 最终值即
     * corrected；学习别名命中且与最终协议一致 → usage_count/streak 递增（达
     * GLOBAL_USAGE_THRESHOLD 沉淀 active）；命中但最终协议不同 → 该别名资产
     * streak 清零（负样本信号，§3.2）。
     */
    collectConfirm(sessionId: string, finals: { recv?: string | null; fwd?: string | null }): void {
        if (!this.ok) return;
        const now = new Date().toISOString();
        const sides: Array<["recv" | "fwd", string, string | null | undefined]> = [
            ["recv", "2", finals.recv],
            ["fwd", "5", finals.fwd],
        ];
        for (const [side, stage, final] of sides) {
            const canonical = (final ?? "").trim();
            if (canonical === "") continue;
            const props = (this.proposals.get(sessionId) ?? []).filter((p) => p.stage === stage);
            const corrected = props.some((p) => p.value !== canonical);
            this.insertEvent({
                stage,
                sessionId,
                kind: "protocol_alias",
                assetKey: canonical,
                proposed: safe_truncate(JSON.stringify(props.map((p) => p.value))),
                final: canonical,
                corrected,
                inputRef: `session:${sessionId}:${side}`,
                at: now,
            });
            const hits = (this.learnedHits.get(sessionId) ?? []).filter(
                (h) => h.canonical === canonical,
            );
            for (const h of hits) {
                this.bump_alias(alias_key_norm(h.alias), now);
            }
            const wrongHits = (this.learnedHits.get(sessionId) ?? []).filter(
                (h) => h.canonical !== canonical,
            );
            for (const h of wrongHits) {
                this.reset_alias_streak(alias_key_norm(h.alias), now);
            }
        }
        this.proposals.delete(sessionId);
        this.learnedHits.delete(sessionId);
        this.invalidate();
    }

    /**
     * 设备资产沉淀（§3.3 阶段 1.5，站点层试用期；2026-10-31 白名单废除裁定——
     * 未知设备询问 → 用户确认 → 接入 → 沉淀）：确认成功 → confirm_streak+1，
     * ≥ SITE_STREAK_THRESHOLD(3) 转正 active。
     */
    recordDeviceConfirm(
        sessionId: string,
        name: string,
        originSite: string,
        description: string,
    ): void {
        if (!this.ok) return;
        const key = alias_key_norm(name);
        if (key === "") return;
        const now = new Date().toISOString();
        const existing = this.findAsset("device_alias", "site", key);
        if (existing) {
            const streak = existing.confirm_streak + 1;
            const status =
                streak >= SITE_STREAK_THRESHOLD && existing.status !== "rejected"
                    ? "active"
                    : existing.status;
            this.db!
                .prepare(
                    "UPDATE assets SET confirm_streak=?, status=?, version=version+1, updated_at=? WHERE id=?",
                )
                .run(streak, status, now, existing.id);
        } else {
            this.db!
                .prepare(
                    "INSERT INTO assets (kind, stage, scope, key, value, status, confirm_streak, " +
                        "origin_site, created_at, updated_at) " +
                        "VALUES ('device_alias', '1.5', 'site', ?, ?, 'probation', 1, ?, ?, ?)",
                )
                .run(
                    key,
                    JSON.stringify({ name, description }),
                    originSite,
                    now,
                    now,
                );
        }
        this.insertEvent({
            stage: "1.5",
            sessionId,
            kind: "device_alias",
            assetKey: key,
            proposed: "(设备接入确认)",
            final: name,
            corrected: false,
            inputRef: `session:${sessionId}`,
            at: now,
        });
        this.invalidate();
    }

    /** 沉淀门控巡检（§3.2）：达标 candidate → active。返回转正数量。 */
    promote(): number {
        if (!this.ok) return 0;
        const now = new Date().toISOString();
        const r = this.db!
            .prepare(
                "UPDATE assets SET status='active', version=version+1, updated_at=? " +
                    "WHERE kind='protocol_alias' AND scope='global' AND status IN ('candidate','probation') " +
                    "AND usage_count >= ?",
            )
            .run(now, GLOBAL_USAGE_THRESHOLD);
        this.invalidate();
        return Number(r.changes);
    }

    close(): void {
        this.proposals.clear();
        this.learnedHits.clear();
        this.db?.close();
        this.db = null;
    }

    // ── 内部 ─────────────────────────────────────────────────

    private findAsset(kind: string, scope: string, key: string): AssetRow | null {
        const row = this.db!.prepare(
            "SELECT id, key, value, status, usage_count, confirm_streak FROM assets " +
                "WHERE kind=? AND scope=? AND key=?",
        ).get(kind, scope, key) as AssetRow | undefined;
        return row ?? null;
    }

    private bump_alias(key: string, now: string): void {
        const row = this.findAsset("protocol_alias", "global", key);
        if (!row) return;
        const usage = row.usage_count + 1;
        const status =
            usage >= GLOBAL_USAGE_THRESHOLD && row.status !== "rejected" ? "active" : row.status;
        this.db!
            .prepare(
                "UPDATE assets SET usage_count=?, confirm_streak=confirm_streak+1, status=?, " +
                    "version=version+1, updated_at=? WHERE id=?",
            )
            .run(usage, status, now, row.id);
        this.invalidate();
    }

    private reset_alias_streak(key: string, now: string): void {
        this.db!
            .prepare(
                "UPDATE assets SET confirm_streak=0, version=version+1, updated_at=? " +
                    "WHERE kind='protocol_alias' AND scope='global' AND key=? AND status IN ('candidate','probation')",
            )
            .run(now, key);
    }

    private insertEvent(e: {
        stage: string;
        sessionId: string;
        kind: string | null;
        assetKey: string | null;
        proposed: string;
        final: string;
        corrected: boolean;
        inputRef: string;
        at: string;
    }): void {
        this.db!
            .prepare(
                "INSERT INTO events (stage, session_id, kind, asset_key, source, input_ref, " +
                    "proposed, final, corrected, confidence, occurred_at) " +
                    "VALUES (?, ?, ?, ?, 'stage', ?, ?, ?, ?, ?, ?)",
            )
            .run(
                e.stage,
                e.sessionId,
                e.kind,
                e.assetKey,
                e.inputRef,
                safe_truncate(e.proposed),
                safe_truncate(e.final),
                e.corrected ? 1 : 0,
                e.corrected ? 0 : 1,
                e.at,
            );
    }
}

/** §4.2 事件体量政策：单字段 64KB 上限，超限存摘要标记。 */
function safe_truncate(s: string): string {
    if (s.length <= EVENT_PAYLOAD_LIMIT) return s;
    return s.slice(0, EVENT_PAYLOAD_LIMIT) + "…[截断:超64KB]";
}

let singleton: AssetLearner | null = null;

/** 单例（进程级）。库打开失败时返回降级 no-op 实例——学习停摆但接入主流程不受影响。 */
export function get_asset_learner(dbPath?: string): AssetLearner {
    if (singleton) return singleton;
    const path =
        dbPath ??
        process.env["C4_ASSETS_DB"] ??
        join(homedir(), ".local", "c4", "assets.db");
    singleton = new AssetLearner(path);
    return singleton;
}
