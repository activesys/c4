// c4/agent/src/subagents/tools/query_abbr_registry.ts — 设备身份注册表检索工具
// 用途：判断目标设备是否已接入（同一设备加点 / 目标不存在 / 同名消歧）。
// 根据 agent.md §3.2.1.3a「id 确定流程」step 2 实现——确定性检索，无 LLM、无网络。
// 2026-10-01 设计修订：注册表条目为「设备名 → {宿主实例, 点前缀}」（channel{N}
// 顺序句柄为实例 id，用户不可见；设备语义在注册表与点 key 前缀）。

import { tool } from "langchain";
import { z } from "zod";
import * as fs from "node:fs";
import * as path from "node:path";

import {
    load_abbr_registry,
    retrieve_device,
    type AbbrRegistry,
} from "../../registry/abbr_registry.js";
import type { SystemConfig } from "../../types/index.js";

// ── 判定标签（确定性，LLM 只翻译 hint 文本）─────────────────

export type AbbrDecision =
    | "hit_add_merge"
    | "no_hit_add_new"
    | "hit_modify"
    | "hit_delete"
    | "no_hit_modify_delete_not_exist"
    | "name_conflict"
    | "site_mismatch"
    | "site_ambiguous";

type SiteAttribution = "ok" | "mismatch" | "ambiguous";

const GROUP_NAMES: string[] = [
    "国家能源", "国家电投", "中电建", "中能建", "中广核",
    "华能", "国能", "大唐", "华电", "国电", "三峡",
    "龙源", "金风", "远景", "明阳", "华润",
];

const SITE_TYPE_WORDS: string[] = [
    "新能源", "水电站", "火电厂", "核电站", "变电站", "风电场",
    "光伏", "储能", "水电", "火电", "核电", "电站", "电厂", "电场", "能源",
];

const DEVICE_TYPE_WORDS: string[] = [
    "升压站", "汇流箱", "开关柜", "配电柜", "测风塔", "箱变", "风机", "机组",
    "逆变器", "断路器", "变压器", "电容器", "电抗器", "储能", "电表",
    "开关", "母线", "杆塔", "PCS", "电池", "柴发", "柴油机",
];

function _strip_leading_groups(text: string): string {
    let result = text;
    for (const g of GROUP_NAMES) {
        if (result.startsWith(g)) {
            result = result.slice(g.length);
            break;
        }
    }
    return result;
}

function _strip_trailing_type(text: string): string {
    let result = text;
    for (const t of SITE_TYPE_WORDS) {
        if (result.endsWith(t)) {
            result = result.slice(0, result.length - t.length);
            break;
        }
    }
    return result;
}

function _core_place_name(text: string): string {
    return _strip_trailing_type(_strip_leading_groups(text)).trim();
}

function _extract_site_prefix(description: string): string {
    const trimmed = description.trim();
    const numMatch = trimmed.match(/(\d+#|\d+号|\d+)/);
    if (numMatch && numMatch.index !== undefined && numMatch.index >= 0) {
        return trimmed.slice(0, numMatch.index).trim();
    }
    let rest = trimmed;
    for (const d of DEVICE_TYPE_WORDS) {
        if (rest.endsWith(d)) {
            rest = rest.slice(0, rest.length - d.length);
            break;
        }
    }
    return rest.trim();
}

function check_site_attribution(
    description: string,
    site: { name: string; abbr: string } | null,
): SiteAttribution {
    if (site === null || site.name.length === 0 || description.trim().length === 0) {
        return "ok";
    }
    const prefix = _extract_site_prefix(description);
    if (prefix.length === 0 || prefix === site.name || prefix === site.abbr) {
        return "ok";
    }
    const site_core = _core_place_name(site.name);
    const prefix_core = _core_place_name(prefix);
    const has_group = GROUP_NAMES.some((g) => prefix.startsWith(g));
    if (has_group) {
        return prefix_core === site_core ? "ok" : "mismatch";
    }
    if (prefix_core === site_core || prefix.includes(site_core)) {
        return "ambiguous";
    }
    return "mismatch";
}

function compute_decision(
    intent: "add" | "modify" | "delete",
    decision: "same_device" | "name_conflict" | "no_hit",
): AbbrDecision {
    if (decision === "same_device") {
        if (intent === "add") return "hit_add_merge";
        return intent === "modify" ? "hit_modify" : "hit_delete";
    }
    if (decision === "name_conflict") {
        return "name_conflict";
    }
    if (intent === "add") {
        return "no_hit_add_new";
    }
    return "no_hit_modify_delete_not_exist";
}

function decision_hint(
    decision: AbbrDecision,
    match: ReturnType<typeof retrieve_device>,
): string {
    switch (decision) {
        case "hit_add_merge":
            return `同一设备已接入（宿主实例 ${match.entry?.host ?? ""}，点 key 前缀 ${match.entry?.prefix ?? ""}_）。` +
                "加点合并到已有设备（宿主实例），不新建——方案确认时用户会对设备身份做最终确认。";
        case "no_hit_add_new":
            return "注册表中无此设备，视为新设备接入：点 key 前缀由设备名确定性派生，经方案确认后固化。";
        case "hit_modify":
            return `目标已接入（宿主实例 ${match.entry?.host ?? ""}，前缀 ${match.entry?.prefix ?? ""}_）。请复述修改内容，询问是否确认修改。`;
        case "hit_delete":
            return `目标已接入（宿主实例 ${match.entry?.host ?? ""}，前缀 ${match.entry?.prefix ?? ""}_）。请复述删除目标，询问是否确认删除。`;
        case "name_conflict":
            return "注册表中存在多条同名设备（现场可能有两台同名设备）——必须追问用户区分：" +
                "请用户提供新设备的名称/编号，或以点 key 前缀/设备描述指认目标。禁止猜测。";
        case "no_hit_modify_delete_not_exist":
            return "查询未命中——注册表中无此设备。你必须原样回复「目标不存在，可能已删除或从未接入」这一句话。" +
                "禁止提及注册表中的其他设备，禁止询问用户任何问题，禁止生成方案，禁止调用任何其他工具。";
        case "site_mismatch":
            return "该资料不属于当前场站。你必须原样回复「该资料不属于当前场站」这一句话——必须包含「不属于」三个字。" +
                "禁止调用任何其他工具，禁止生成方案，禁止继续接入流程。";
        case "site_ambiguous":
            return "资料中的场站归属不明，可能多个场站共用一份点表。你必须提醒用户确认场站归属——" +
                "回复必须包含「归属」和「确认」二字（例如「请确认该资料的场站归属」）。" +
                "禁止调用任何其他工具，禁止生成方案。";
    }
}

// ── 工厂函数 ──────────────────────────────────────────────

export function createQueryAbbrRegistryTool(opts: {
    configPath: string;
    agentConfigPath: string;
    site?: { name: string; abbr: string } | null;
}) {
    return tool(
        async ({ description, intent }: {
            description: string;
            intent: "add" | "modify" | "delete";
        }) => {
            const registry_path = path.join(
                path.dirname(opts.configPath),
                "abbr_registry.json",
            );

            let config_json: SystemConfig | undefined;
            try {
                config_json = JSON.parse(
                    fs.readFileSync(opts.configPath, "utf-8"),
                ) as SystemConfig;
            } catch {
                config_json = undefined;
            }

            let site = opts.site ?? null;
            if (!site) {
                try {
                    const agent_cfg = JSON.parse(
                        fs.readFileSync(opts.agentConfigPath, "utf-8"),
                    ) as Record<string, unknown>;
                    const s = agent_cfg["site"] as
                        | { name?: unknown; abbr?: unknown }
                        | undefined;
                    if (
                        s &&
                        typeof s.name === "string" &&
                        typeof s.abbr === "string"
                    ) {
                        site = { name: s.name, abbr: s.abbr };
                    }
                } catch {
                    site = null;
                }
            }

            const registry: AbbrRegistry = await load_abbr_registry(
                registry_path,
                config_json,
            );
            if (intent === "add") {
                const attribution = check_site_attribution(description, site);
                if (attribution !== "ok") {
                    const decision: AbbrDecision =
                        attribution === "mismatch" ? "site_mismatch" : "site_ambiguous";
                    return JSON.stringify({
                        success: true,
                        entries: registry.entries,
                        match: { decision: "no_hit" },
                        decision,
                        hint: decision_hint(decision, { decision: "no_hit" }),
                    });
                }
            }
            const match = retrieve_device(registry, description, description);
            const decision = compute_decision(intent, match.decision);
            const hint = decision_hint(decision, match);

            return JSON.stringify({
                success: true,
                entries: registry.entries,
                match: {
                    decision: match.decision,
                    ...(match.entry
                        ? {
                              name: match.entry.name,
                              prefix: match.entry.prefix,
                              host: match.entry.host,
                          }
                        : {}),
                    ...(match.candidates
                        ? {
                              candidates: match.candidates.map((c) => ({
                                  name: c.name,
                                  prefix: c.prefix,
                                  host: c.host,
                              })),
                          }
                        : {}),
                },
                decision,
                hint,
            });
        },
        {
            name: "query_abbr_registry",
            description:
                "检索设备身份注册表，判断目标设备是否已接入。返回已接入设备列表（entries：设备名/宿主实例/点 key 前缀）、" +
                "匹配结果（match）、判定标签（decision）和应执行的行动提示（hint）。" +
                "add/modify/delete 操作前必须调用本工具。" +
                "参数 description 为目标设备名称/描述（如「1号风机」），intent 为操作意图。",
            schema: z.object({
                description: z.string().describe("目标设备名称或描述，如「1号风机」"),
                intent: z
                    .enum(["add", "modify", "delete"])
                    .describe("操作意图：add 接入 / modify 修改 / delete 删除"),
            }),
        },
    );
}
