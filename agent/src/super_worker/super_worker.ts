// c4/agent/src/super_worker/super_worker.ts — SuperWorker 组装
// createAgent 配置 + streamEvents v3 + 执行器工具注入 + AgentState 接线 + abbr 记忆库固化

import { createAgent } from "langchain";
import type { StructuredTool } from "@langchain/core/tools";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import type { McpServiceRegistry } from "../registry/registry.js";
import type { C4McpManager } from "../mcp/client.js";
import { xlsxParserTool, csvParserTool, txtParserTool } from "../subagents/tools/doc_parsers.js";
import { createOutputPlanStepsTool } from "../subagents/tools/output_plan_steps.js";
import { outputAccessPlanTool } from "../subagents/tools/output_access_plan.js";
import { outputDeviceInfoTool } from "../subagents/tools/output_device_info.js";
import { createQueryRegistryTool } from "../subagents/tools/query_registry.js";
import { createQueryAbbrRegistryTool } from "../subagents/tools/query_abbr_registry.js";
import {
    merge_config_from_steps,
    runRuntimeStopStart,
} from "../executor/executor.js";
import {
    load_abbr_registry,
    save_abbr_registry,
    finalize_entry,
    delete_entry,
} from "../registry/abbr_registry.js";
import type { C4Agent, AgentStateWriter } from "../server/types.js";
import type { ServiceStep, SystemConfig } from "../types/index.js";
import type { AgentLogger } from "../logging/agent_logger.js";

// ── 类型 ──────────────────────────────────────────────────

export interface SuperWorkerConfig {
    model: unknown;
    registry: McpServiceRegistry;
    mcpManager: C4McpManager;
    configPath: string;
    agentConfigPath: string;
    instanceId: string;
    site?: { name: string; abbr: string } | null;
    state?: AgentStateWriter;
    agentLogger?: AgentLogger;
}

// ── 系统提示 ──────────────────────────────────────────────

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const SYSTEM_PROMPT_PATH = path.join(__dirname, "prompts", "system.txt");

function loadSystemPrompt(
    registry: McpServiceRegistry,
    site?: { name: string; abbr: string } | null,
): string {
    let template: string;
    try {
        template = fs.readFileSync(SYSTEM_PROMPT_PATH, "utf-8");
    } catch {
        throw new Error(`SuperWorker 系统提示模板未找到: ${SYSTEM_PROMPT_PATH}`);
    }
    const catalog = registry.isLoaded
        ? registry.getServiceCatalog()
        : "暂无可用服务。";
    const currentSite = site
        ? `${site.name}（缩写 ${site.abbr}）`
        : "（未设置）";
    // 场站规则按绑定状态条件渲染（agent.md §3.2.1.3：归属校验确定性执行，不依赖 LLM 自选分支）
    const siteRules = site
        ? [
            `- **场站归属（已绑定：${site.name}，缩写 ${site.abbr}）——硬性要求，必须先于 output_device_info 执行**：`,
            `  - 本实例与场站「${site.name}」永久绑定：**禁止以任何理由向用户询问场站名称或缩写**，即使消息中没有场站信息，也直接按当前场站处理。`,
            `  - 用户消息未提及其他场站名称 → 一律默认属于当前场站，直接继续解析，不做任何场站确认询问。`,
            `  - 消息中设备名带场站前缀且前缀与「${site.name}」一致 → 继续处理。`,
            `  - 消息明确出现其他场站名称（不同地名，如「华能大青山」）或「场站名称：其他场站」→ 回复「该资料不属于当前场站（当前场站：${site.name}）」并停止，不调用任何工具。`,
        ].join("\n")
        : [
            `- **场站归属（首次接入，硬性要求，必须先于 output_device_info 执行）**：`,
            `  - 当前场站未设置：**必须先询问「请提供场站名称（如：场站名称：华能阿拉善）」**——只询问场站名称，**不要询问缩写**（缩写由你按拼音首字母自动生成，如 华能阿拉善→hnals、开鲁→kl），不要调用 output_device_info。`,
            `  - 用户提供场站名称后 → 生成缩写并回复「场站：{名称}（缩写 {缩写}）」记住它，后续接入默认使用该场站、不再重复询问，然后继续解析点表或输出设备信息。`,
        ].join("\n");
    // 使用函数形式替换，避免 site/目录内容中的 $ 序列被解释为替换模式
    return template
        .replaceAll("{{ service_catalog }}", () => catalog)
        .replaceAll("{{ current_site }}", () => currentSite)
        .replaceAll("{{ site_rules }}", () => siteRules);
}

// ── SuperWorker 工厂 ──────────────────────────────────────

export async function createSuperWorker(
    config: SuperWorkerConfig,
): Promise<ReturnType<typeof createAgent>> {
    const { model, registry, mcpManager } = config;
    const systemPrompt = loadSystemPrompt(registry, config.site);

    const mcpTools: StructuredTool[] = await mcpManager.getTools();

    const allTools: StructuredTool[] = [
        xlsxParserTool,
        csvParserTool,
        txtParserTool,
        outputDeviceInfoTool,
        outputAccessPlanTool,
        createOutputPlanStepsTool(registry, config.site, config.configPath),
        createQueryRegistryTool(registry),
        createQueryAbbrRegistryTool({
            configPath: config.configPath,
            agentConfigPath: config.agentConfigPath,
            site: config.site,
        }),
    ];

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const agent = createAgent({
        model: model as any,
        tools: allTools,
        systemPrompt,
    } as any) as any;

    return agent;
}

// ── abbr 记忆库固化（agent.md §3.2.1.3a step4）────────────
// 确认执行后，将 <描述, id> 写入记忆库；delete 时物理删除。

async function persist_abbr_registry(
    planSteps: ServiceStep[],
    deviceInfo: Record<string, unknown> | null,
    accessPlan: Record<string, unknown> | null,
    config: SuperWorkerConfig,
): Promise<void> {
    try {
        const registry_path = path.join(
            path.dirname(config.configPath),
            "abbr_registry.json",
        );

        let config_json: SystemConfig | undefined;
        try {
            const raw = fs.readFileSync(config.configPath, "utf-8");
            config_json = JSON.parse(raw) as SystemConfig;
        } catch {
            config_json = undefined;
        }

        let registry = await load_abbr_registry(registry_path, config_json);

        const data = accessPlan ?? deviceInfo;
        const site_raw = (data as Record<string, unknown> | null)?.site as
            | Record<string, unknown>
            | null;
        const site_abbr = site_raw && typeof site_raw.abbr === "string" && site_raw.abbr
            ? site_raw.abbr
            : (config.site?.abbr ?? null);
        const site_name = site_raw && typeof site_raw.name === "string"
            ? site_raw.name
            : (config.site?.name ?? null);
        const site = site_abbr && site_name
            ? { name: site_name, abbr: site_abbr }
            : null;

        if (site) {
            persist_site_to_agent_config(config.agentConfigPath, site);
            registry = await load_abbr_registry(registry_path, config_json, site);
        }

        const prefix = site_abbr ? `${site_abbr}_` : "";

        let next = registry;
        for (const step of planSteps) {
            const id = typeof step.instance.id === "string" ? step.instance.id : "";
            if (!id) continue;
            if (step.action === "add") {
                const entry = config.registry.queryRegistry(step.service_type);
                const name =
                    typeof step.instance.name === "string" ? step.instance.name : id;
                const abbr = prefix && id.startsWith(prefix) ? id.slice(prefix.length) : id;
                next = finalize_entry(next, {
                    id,
                    name,
                    abbr,
                    service_type: step.service_type,
                    role: entry?.role ?? null,
                    description: name,
                });
            } else if (step.action === "delete") {
                next = delete_entry(next, id);
            }
        }
        await save_abbr_registry(next, registry_path);
    } catch {
        // 记忆库写入失败不影响主流程（记忆库是可重建的派生数据，config.json 是权威）
    }
}

function persist_site_to_agent_config(
    agent_config_path: string,
    site: { name: string; abbr: string },
): void {
    try {
        const raw = fs.readFileSync(agent_config_path, "utf-8");
        const cfg = JSON.parse(raw) as Record<string, unknown>;
        cfg.site = site;
        fs.writeFileSync(
            agent_config_path,
            JSON.stringify(cfg, null, 4) + "\n",
            "utf-8",
        );
    } catch {
        // site 固化失败不影响主流程（agent.json 可被后续接入重新固化）
    }
}

// ── C4Agent 包装（streamEvents v3）────────────────────────

export async function createC4Agent(
    config: SuperWorkerConfig,
): Promise<C4Agent> {
    const agent = await createSuperWorker(config);

    // 跨轮持久化的设备信息与接入方案——"生成方案"轮产出，"确认"轮注入，
    // 避免依赖 LLM 从历史重新推导（追加设备/修改/删除场景易出错）。
    let deviceInfo: Record<string, unknown> | null = null;
    let accessPlan: Record<string, unknown> | null = null;

    return {
        invoke: async function* (input) {
            const conversation = input.conversationId ?? "unknown";
            const log = config.agentLogger;
            let planSteps: ServiceStep[] | null = null;
            let planDeviceInfo: Record<string, unknown> | null = null;
            let planAccessPlan: Record<string, unknown> | null = null;
            let confirmOriginalContent: string | null = null;
            let isConfirm = false;
            try {
                if (input.messages.length > 0) {
                    const last = input.messages[input.messages.length - 1];
                    log?.user_input(conversation, last.role, String(last.content));
                    const lastContent = last.content as string;
                    // 否定/拒绝词优先判断，避免 "取消，不执行..." 中的 "执行" 被误判为确认
                    const isReject = /取消|拒绝|放弃|停止|算了|不执行|不要执行|不确认/.test(lastContent);
                    if (last.role === "user" && !isReject) {
                        const siteNameMatch = lastContent.match(
                            /场站名称[:：]\s*([^,，\n]+)/,
                        );
                        if (config.site && siteNameMatch) {
                            const provided = siteNameMatch[1].trim();
                            if (provided !== config.site.name) {
                                yield {
                                    type: "text" as const,
                                    content: `该资料不属于当前场站（当前场站：${config.site.name}）`,
                                };
                                return;
                            }
                        } else if (!config.site && siteNameMatch) {
                            const abbrMatch = lastContent.match(/缩写[:：]\s*(\S+)/);
                            if (abbrMatch) {
                                config.site = { name: siteNameMatch[1].trim(), abbr: abbrMatch[1].trim() };
                                persist_site_to_agent_config(config.agentConfigPath, config.site);
                                yield {
                                    type: "text" as const,
                                    content: `已记录场站：${config.site.name}（缩写 ${config.site.abbr}）`,
                                };
                                return;
                            }
                            // 仅提供场站名称（无缩写）→ 交由 LLM 生成缩写并确认（按 site_rules 首次接入分支）
                        } else if (!config.site && /华能|风电场|电场|场站/.test(lastContent)) {
                            yield {
                                type: "text" as const,
                                content: "请提供场站名称（如：场站名称：华能阿拉善）",
                            };
                            return;
                        }
                    }
                    if (last.role === "user" && !isReject && /确认|好的|执行|按方案|开始/.test(lastContent)) {
                        confirmOriginalContent = lastContent;
                        isConfirm = true;
                        config.state?.setPhase("confirmed");
                        log?.phase(conversation, "confirmed");
                        // 先同步 accessPlan 的 site 到 config.site（首次接入时 config.site 还是 null，
                        // 供「确定性生成」兜底与 output_plan_steps 的 fallback 使用权威 site）
                        if (accessPlan && !config.site) {
                            const ap_site = (accessPlan as Record<string, unknown>).site as
                                | { name?: unknown; abbr?: unknown }
                                | undefined;
                            if (
                                ap_site &&
                                typeof ap_site.name === "string" &&
                                typeof ap_site.abbr === "string"
                            ) {
                                config.site = { name: ap_site.name, abbr: ap_site.abbr };
                            }
                        }
                        // 将 deviceInfo + accessPlan 注入消息，供 LLM 传入 output_plan_steps
                        const extra = [];
                        if (deviceInfo) extra.push(`设备信息: ${JSON.stringify(deviceInfo)}`);
                        if (accessPlan) extra.push(`接入方案: ${JSON.stringify(accessPlan)}`);
                        const siteHint = config.site
                            ? `（site 必须原样使用 ${JSON.stringify(config.site)}）`
                            : "";
                        const prefix = extra.length > 0 ? `\n\n以下信息供你调用 output_plan_steps 时使用:\n${extra.join("\n")}\n\n` : "";
                        input = {
                            messages: [...input.messages.slice(0, -1), {
                                role: "user" as const,
                                content: `${lastContent}${prefix}立即调用 output_plan_steps 工具执行确认的变更（新增接入传 devices/site/forward_targets${siteHint}，修改/删除传 changes），不要用文字回答。`,
                            }],
                        };
                        planDeviceInfo = deviceInfo;
                        planAccessPlan = accessPlan;
                        // 重置，避免下次 confirm 重复注入旧数据
                        deviceInfo = null;
                        accessPlan = null;
                    }
                }

                const MAX_MISSES = 3;
                let misses = 0;

                while (misses <= MAX_MISSES) {
                    let hasToolCall = false;

                    log?.llm_call(conversation, misses + 1, input.messages);

                    const stream = await (agent as any).streamEvents(
                        { messages: input.messages },
                        { version: "v3", configurable: { recursion_limit: 50 } },
                    );

                    const toolResults: Array<{ name: string; result: string }> = [];
                    const bgCapture = (async () => {
                        for await (const call of stream.toolCalls) {
                            try {
                                const out = await call.output;
                                const r = typeof out === "string" ? JSON.parse(out) : out;
                                if (call.name === "output_plan_steps" && r?.success && Array.isArray(r?.steps)) {
                                    planSteps = r.steps as ServiceStep[];
                                }
                                if (call.name === "output_access_plan" && r?.success) {
                                    accessPlan = r;
                                    config.state?.setPhase("planning");
                                    log?.phase(conversation, "planning");
                                    config.state?.setAccessPlan(true);
                                }
                                if (call.name === "output_device_info" && r?.success && Array.isArray(r?.devices)) {
                                    deviceInfo = r;
                                    config.state?.setPhase("collecting");
                                    log?.phase(conversation, "collecting");
                                }
                                const resultStr = typeof out === "string" ? out : JSON.stringify(out);
                                toolResults.push({ name: call.name, result: resultStr });
                                log?.tool_result(conversation, call.name, resultStr);
                            } catch { /* ignore */ }
                        }
                    })();

                    for await (const msg of stream.messages) {
                        const textParts: string[] = [];
                        for await (const token of msg.text) {
                            textParts.push(token);
                            yield { type: "text" as const, content: token };
                        }
                        if (textParts.length > 0) {
                            log?.llm_text(conversation, textParts.join(""));
                        }
                        for await (const tc of msg.toolCalls) {
                            const tcArgs = (tc as unknown as { args?: unknown }).args ?? {};
                            yield { type: "tool_call" as const, name: tc.name, args: {} };
                            log?.tool_call(conversation, tc.name, tcArgs);
                            hasToolCall = true;
                        }
                    }

                    await bgCapture;
                    for (const tr of toolResults) {
                        yield { type: "tool_result" as const, name: tr.name, result: tr.result };
                    }

                    if (hasToolCall || misses >= MAX_MISSES || !isConfirm) break;

                    misses++;
                    input.messages = [...input.messages, {
                        role: "user" as const,
                        content: "你必须立即调用 output_plan_steps 工具。不要用文字回答。",
                    }];
                }

                // 确定性优先：若确认消息嵌入了 changes JSON，直接采用（覆盖 LLM 的非确定性 id 映射）
                if (isConfirm && confirmOriginalContent) {
                    const changesMatch = confirmOriginalContent.match(/\{[\s\S]*"changes"[\s\S]*\}/);
                    if (changesMatch) {
                        try {
                            const cd = JSON.parse(changesMatch[0]);
                            if (Array.isArray(cd.changes)) {
                                planSteps = cd.changes as ServiceStep[];
                            }
                        } catch { /* ignore */ }
                    }
                }

                if (isConfirm) {
                    let data: any = planAccessPlan || planDeviceInfo;
                    const pdi_devices = (planDeviceInfo as Record<string, unknown> | null)?.devices as unknown[] | undefined;
                    if ((!data?.devices || (data.devices as unknown[]).length === 0) && pdi_devices && pdi_devices.length > 0) {
                        data = { ...(data ?? {}), devices: pdi_devices };
                    }
                    if (!data?.devices && confirmOriginalContent) {
                        const jsonMatch = confirmOriginalContent.match(/\{[\s\S]*"devices"[\s\S]*\}/);
                        if (jsonMatch) {
                            try { data = JSON.parse(jsonMatch[0]); } catch { /* ignore */ }
                        }
                    }
                    // 转发兜底：历史消息含「转发」但方案缺失 forward_targets 时补中心侧，保证 writer/reader 成对
                    const historyText = input.messages
                        .map((m) => (typeof m.content === "string" ? m.content : ""))
                        .join("\n");
                    if (
                        data?.devices &&
                        Array.isArray(data.devices) &&
                        data.devices.length > 0 &&
                        (!data.forward_targets ||
                            !Array.isArray(data.forward_targets) ||
                            data.forward_targets.length === 0) &&
                        /转发/.test(historyText)
                    ) {
                        data = {
                            ...(data ?? {}),
                            forward_targets: [
                                { name: "中心侧", abbr: "center", protocol: "asfp2", ip: "172.16.109.11", port: 9999 },
                            ],
                        };
                    }
                    if (data?.devices) {
                        try {
                            const genTool = createOutputPlanStepsTool(config.registry, config.site);
                            const raw = await (genTool as any).invoke(data);
                            const r = typeof raw === "string" ? JSON.parse(raw) : raw;
                            if (r?.success && Array.isArray(r?.steps)) {
                                planSteps = r.steps as ServiceStep[];
                            }
                        } catch { /* deterministic generation failed, skip */ }
                    }
                }

                if (planSteps && planSteps.length > 0) {
                    config.state?.setPhase("executing");
                    log?.phase(conversation, "executing");
                    try {
                        const mr = await merge_config_from_steps(planSteps, config.configPath, config.registry as any);
                        log?.memory(conversation, "config_merge", {
                            success: mr.success,
                            error: mr.error ?? null,
                            warnings: mr.warnings,
                        });
                        if (mr.success) {
                            yield { type: "text" as const, content: "接入方案已执行，配置已写入。" };

                            const ssr = await runRuntimeStopStart(
                                config.mcpManager.getMultiClient(),
                                "shm",
                                config.instanceId,
                                config.configPath,
                                config.registry as any,
                            );
                            log?.memory(conversation, "stop_start", {
                                success: ssr.success,
                                started_services: ssr.started_services,
                                failed_services: ssr.failed_services.map((f) => ({
                                    service_type: f.service_type,
                                    error: f.error,
                                })),
                                abort_reason: ssr.abort_reason ?? null,
                            });
                            if (!(ssr.abort_reason && /配置类错误/.test(ssr.abort_reason))) {
                                await persist_abbr_registry(
                                    planSteps,
                                    planDeviceInfo,
                                    planAccessPlan,
                                    config,
                                );
                                log?.memory(conversation, "abbr_persist", {
                                    plan_steps: planSteps.length,
                                });
                            }
                            if (ssr.success) {
                                yield {
                                    type: "text" as const,
                                    content: `服务已重启: ${ssr.started_services.join(", ")}`,
                                };
                            } else if (ssr.abort_reason) {
                                log?.error(conversation, ssr.abort_reason);
                                yield {
                                    type: "error" as const,
                                    message: ssr.abort_reason,
                                };
                            }
                            if (ssr.failed_services.length > 0) {
                                const names = ssr.failed_services.map((f) => f.service_type).join(", ");
                                const details = ssr.failed_services
                                    .map((f) => `${f.service_type}: ${f.error}`)
                                    .join("; ");
                                log?.error(conversation, `部分服务启动失败: ${details}`);
                                yield {
                                    type: "error" as const,
                                    message: `部分服务启动失败: ${names}`,
                                };
                            }
                            config.state?.setPhase("idle");
                            log?.phase(conversation, "idle");
                            config.state?.setAccessPlan(false);
                        } else {
                            log?.error(conversation, `执行问题: ${mr.error ?? "未知"}`);
                            yield { type: "text" as const, content: `执行问题: ${mr.error ?? "未知"}` };
                            config.state?.setError(mr.error ?? "未知");
                        }
                    } catch (ex: unknown) {
                        const exMsg = ex instanceof Error ? ex.message : String(ex);
                        log?.error(conversation, `自动执行失败: ${exMsg}`);
                        yield {
                            type: "error" as const,
                            message: `自动执行失败: ${exMsg}`,
                        };
                        config.state?.setError(exMsg);
                    }
                }
                log?.done(conversation);
                yield { type: "done" as const };
            } catch (err: unknown) {
                const errMsg = err instanceof Error ? err.message : String(err);
                log?.error(conversation, errMsg);
                yield {
                    type: "error" as const,
                    message: errMsg,
                };
                config.state?.setError(errMsg);
            }
        },
    };
}
