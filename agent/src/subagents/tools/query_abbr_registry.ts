// c4/agent/src/subagents/tools/query_abbr_registry.ts — abbr 记忆库检索工具
// info-gatherer 用：判断目标设备是否已接入（同一设备加点 / 目标不存在）。
// 根据 agent.md §3.2.1.3a「id 确定流程」step 2 实现——确定性检索，无 LLM、无网络。

import { tool } from "langchain";
import { z } from "zod";
import * as fs from "node:fs";
import * as path from "node:path";

import {
    load_abbr_registry,
    retrieve_candidate,
    type AbbrRegistry,
    type RetrieveCandidateResult,
} from "../../registry/abbr_registry.js";
import type { SystemConfig } from "../../types/index.js";

// ── 判定标签（确定性，LLM 只翻译 hint 文本）─────────────────

export type AbbrDecision =
    | "hit_add_merge"
    | "no_hit_add_new"
    | "hit_modify"
    | "hit_delete"
    | "no_hit_modify_delete_not_exist";

function compute_decision(
    intent: "add" | "modify" | "delete",
    hit: boolean,
): AbbrDecision {
    if (hit) {
        if (intent === "add") {
            return "hit_add_merge";
        }
        return intent === "modify" ? "hit_modify" : "hit_delete";
    }
    if (intent === "add") {
        return "no_hit_add_new";
    }
    return "no_hit_modify_delete_not_exist";
}

function decision_hint(decision: AbbrDecision, match: RetrieveCandidateResult): string {
    switch (decision) {
        case "hit_add_merge":
            return `同一设备已接入（实例 id=${match.id}）。你必须原样询问用户「是否在 ${match.id} 上加点？」——` +
                "这句话必须包含「加点」二字和实例 id，不得改成「新增数据点」等其他说法。加点合并到已有实例，不新建。";
        case "no_hit_add_new":
            return "记忆库中无此设备，视为新设备接入，使用候选 abbr（如 1#风机→wt1）。";
        case "hit_modify":
            return `目标已接入（实例 id=${match.id}）。请复述修改内容（可提及该实例 id），询问是否确认修改。`;
        case "hit_delete":
            return `目标已接入（实例 id=${match.id}）。请复述删除目标（可提及该实例 id），询问是否确认删除。`;
        case "no_hit_modify_delete_not_exist":
            return "查询未命中——记忆库中无此设备。你必须原样回复「目标不存在，可能已删除或从未接入」这一句话。" +
                "禁止提及记忆库中的其他设备，禁止询问用户任何问题，禁止生成方案，禁止调用任何其他工具。";
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
                site,
            );
            const match = retrieve_candidate(registry, { description, intent });
            const decision = compute_decision(intent, match.hit);
            const hint = decision_hint(decision, match);

            return JSON.stringify({
                success: true,
                entries: registry.entries,
                match,
                decision,
                hint,
            });
        },
        {
            name: "query_abbr_registry",
            description:
                "检索 abbr 记忆库，判断目标设备是否已接入。返回已接入设备列表（entries）、" +
                "描述匹配结果（match）、判定标签（decision）和应执行的行动提示（hint）。" +
                "add/modify/delete 操作前必须调用本工具。" +
                "参数 description 为目标设备名称/描述（如「1#风机」），intent 为操作意图。",
            schema: z.object({
                description: z.string().describe("目标设备名称或描述，如「1#风机」"),
                intent: z
                    .enum(["add", "modify", "delete"])
                    .describe("操作意图：add 接入 / modify 修改 / delete 删除"),
            }),
        },
    );
}
