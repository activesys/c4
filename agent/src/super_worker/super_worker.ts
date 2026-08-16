// c4/agent/src/super_worker/super_worker.ts — SuperWorker 组装
// createAgent 配置 + streamEvents v3 + 执行器工具注入

import { createAgent } from "langchain";
import type { StructuredTool } from "@langchain/core/tools";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import type { McpServiceRegistry } from "../registry/registry.js";
import type { C4McpManager } from "../mcp/client.js";
import { ExecuteAccessPlanTool } from "../subagents/tools/executor.js";
import { xlsxParserTool, csvParserTool, txtParserTool } from "../subagents/tools/doc_parsers.js";
import { createOutputPlanStepsTool } from "../subagents/tools/output_plan_steps.js";
import { outputAccessPlanTool } from "../subagents/tools/output_access_plan.js";
import { outputDeviceInfoTool } from "../subagents/tools/output_device_info.js";
import { createQueryRegistryTool } from "../subagents/tools/query_registry.js";
import {
    merge_config_from_steps,
    runRuntimeStopStart,
} from "../executor/executor.js";
import type { C4Agent } from "../server/types.js";
import type { ServiceStep } from "../types/index.js";

// ── 类型 ──────────────────────────────────────────────────

export interface SuperWorkerConfig {
    model: unknown;
    registry: McpServiceRegistry;
    mcpManager: C4McpManager;
    configPath: string;
    instanceId: string;
}

// ── 系统提示 ──────────────────────────────────────────────

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const SYSTEM_PROMPT_PATH = path.join(__dirname, "prompts", "system.txt");

function loadSystemPrompt(registry: McpServiceRegistry): string {
    let template: string;
    try {
        template = fs.readFileSync(SYSTEM_PROMPT_PATH, "utf-8");
    } catch {
        throw new Error(`SuperWorker 系统提示模板未找到: ${SYSTEM_PROMPT_PATH}`);
    }
    const catalog = registry.isLoaded
        ? registry.getServiceCatalog()
        : "暂无可用服务。";
    return template.replace("{{ service_catalog }}", catalog);
}

// ── SuperWorker 工厂 ──────────────────────────────────────

export async function createSuperWorker(
    config: SuperWorkerConfig,
): Promise<ReturnType<typeof createAgent>> {
    const { model, registry, mcpManager, configPath } = config;
    const systemPrompt = loadSystemPrompt(registry);

    const mcpTools: StructuredTool[] = await mcpManager.getTools();

    const executorTool = new ExecuteAccessPlanTool(configPath, registry as any);

    const allTools: StructuredTool[] = [
        xlsxParserTool,
        csvParserTool,
        txtParserTool,
        outputDeviceInfoTool,
        outputAccessPlanTool,
        createOutputPlanStepsTool(registry),
        createQueryRegistryTool(registry),
    ];

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const agent = createAgent({
        model: model as any,
        tools: allTools,
        systemPrompt,
    } as any) as any;

    return agent;
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
            let planSteps: ServiceStep[] | null = null;
            let planDeviceInfo: Record<string, unknown> | null = null;
            let planAccessPlan: Record<string, unknown> | null = null;
            let confirmOriginalContent: string | null = null;
            let isConfirm = false;
            try {
                if (input.messages.length > 0) {
                    const last = input.messages[input.messages.length - 1];
                    const lastContent = last.content as string;
                    // 否定/拒绝词优先判断，避免 "取消，不执行..." 中的 "执行" 被误判为确认
                    const isReject = /取消|拒绝|放弃|停止|算了|不执行|不要执行|不确认/.test(lastContent);
                    if (last.role === "user" && !isReject && /确认|好的|执行|按方案|开始/.test(lastContent)) {
                        confirmOriginalContent = lastContent;
                        isConfirm = true;
                        // 将 deviceInfo + accessPlan 注入消息，供 LLM 传入 output_plan_steps
                        const extra = [];
                        if (deviceInfo) extra.push(`设备信息: ${JSON.stringify(deviceInfo)}`);
                        if (accessPlan) extra.push(`接入方案: ${JSON.stringify(accessPlan)}`);
                        const prefix = extra.length > 0 ? `\n\n以下信息供你调用 output_plan_steps 时使用:\n${extra.join("\n")}\n\n` : "";
                        input = {
                            messages: [...input.messages.slice(0, -1), {
                                role: "user" as const,
                                content: `${lastContent}${prefix}立即调用 output_plan_steps 工具执行确认的变更（新增接入传 devices/site/forward_targets，修改/删除传 changes），不要用文字回答。`,
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
                                }
                                if (call.name === "output_device_info" && r?.success && Array.isArray(r?.devices)) {
                                    deviceInfo = r;
                                }
                                toolResults.push({ name: call.name, result: typeof out === "string" ? out : JSON.stringify(out) });
                            } catch { /* ignore */ }
                        }
                    })();

                    for await (const msg of stream.messages) {
                        for await (const token of msg.text) {
                            yield { type: "text" as const, content: token };
                        }
                        for await (const tc of msg.toolCalls) {
                            yield { type: "tool_call" as const, name: tc.name, args: {} };
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

                if (isConfirm && (!planSteps || (planSteps as ServiceStep[]).length === 0)) {
                    let data: any = planDeviceInfo || planAccessPlan;
                    if (!data?.devices && confirmOriginalContent) {
                        const jsonMatch = confirmOriginalContent.match(/\{[\s\S]*"devices"[\s\S]*\}/);
                        if (jsonMatch) {
                            try { data = JSON.parse(jsonMatch[0]); } catch { /* ignore */ }
                        }
                    }
                    if (data?.devices) {
                        try {
                            const genTool = createOutputPlanStepsTool(config.registry);
                            const raw = await (genTool as any).invoke(data);
                            const r = typeof raw === "string" ? JSON.parse(raw) : raw;
                            if (r?.success && Array.isArray(r?.steps)) {
                                planSteps = r.steps as ServiceStep[];
                            }
                        } catch { /* deterministic generation failed, skip */ }
                    }
                }

                if (planSteps && planSteps.length > 0) {
                    try {
                        const mr = await merge_config_from_steps(planSteps, config.configPath, config.registry as any);
                        if (mr.success) {
                            yield { type: "text" as const, content: "接入方案已执行，配置已写入。" };

                            const ssr = await runRuntimeStopStart(
                                config.mcpManager.getMultiClient(),
                                "shm",
                                config.instanceId,
                                config.configPath,
                                config.registry as any,
                            );
                            if (ssr.success) {
                                yield {
                                    type: "text" as const,
                                    content: `服务已重启: ${ssr.started_services.join(", ")}`,
                                };
                            } else if (ssr.abort_reason) {
                                yield {
                                    type: "error" as const,
                                    message: ssr.abort_reason,
                                };
                            }
                            if (ssr.failed_services.length > 0) {
                                const names = ssr.failed_services.map((f) => f.service_type).join(", ");
                                yield {
                                    type: "error" as const,
                                    message: `部分服务启动失败: ${names}`,
                                };
                            }
                        } else {
                            yield { type: "text" as const, content: `执行问题: ${mr.error ?? "未知"}` };
                        }
                    } catch (ex: unknown) {
                        yield {
                            type: "error" as const,
                            message: `自动执行失败: ${ex instanceof Error ? ex.message : String(ex)}`,
                        };
                    }
                }
                yield { type: "done" as const };
            } catch (err: unknown) {
                yield {
                    type: "error" as const,
                    message: err instanceof Error ? err.message : String(err),
                };
            }
        },
    };
}
