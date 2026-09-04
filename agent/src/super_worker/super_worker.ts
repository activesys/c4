// c4/agent/src/super_worker/super_worker.ts — SuperWorker 组装
// createAgent 配置 + streamEvents v3 + 执行器工具注入 + AgentState 接线 + abbr 记忆库固化

import { createAgent } from "langchain";
import type { StructuredTool } from "@langchain/core/tools";
import {
    isAIMessage,
    isHumanMessage,
    isSystemMessage,
    isToolMessage,
    type BaseMessage,
} from "@langchain/core/messages";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import type { McpServiceRegistry } from "../registry/registry.js";
import type { C4McpManager } from "../mcp/client.js";
import { xlsxParserTool, csvParserTool, txtParserTool } from "../subagents/tools/doc_parsers.js";
import { createOutputPlanStepsTool, normalize_protocol } from "../subagents/tools/output_plan_steps.js";
import { sanitize_identifier } from "../executor/executor.js";
import { buildErrorTranslator, translateError } from "../mcp/tools.js";
import { createOutputAccessPlanTool } from "../subagents/tools/output_access_plan.js";
import { createOutputDeviceInfoTool } from "../subagents/tools/output_device_info.js";
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
    /** 用户在对话中显式声明的通信协议（规范化后，如 "asfp2"）；null = 尚未声明 */
    declared_protocol?: string | null;
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
        createOutputDeviceInfoTool(registry, config),
        createOutputAccessPlanTool(registry),
        createOutputPlanStepsTool(registry, config.site),
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

// output_access_plan 输出（connection 嵌套）→ output_plan_steps 输入（字段平铺）
function access_plan_to_steps_input(r: Record<string, unknown>): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    if (r.site && typeof r.site === "object") {
        out.site = r.site;
    }
    if (Array.isArray(r.devices)) {
        out.devices = (r.devices as Record<string, unknown>[]).map((d) => ({
            name: d.name,
            abbr: d.abbr ?? sanitize_identifier(String(d.name ?? "")),
            protocol: d.protocol,
            ...(typeof d.connection === "object" && d.connection !== null ? d.connection : {}),
            points: Array.isArray(d.points) ? d.points : [],
        }));
    }
    if (Array.isArray(r.forward_targets)) {
        out.forward_targets = (r.forward_targets as Record<string, unknown>[]).map((t) => ({
            name: t.name,
            abbr: t.abbr ?? sanitize_identifier(String(t.name ?? "")),
            protocol: t.protocol,
            ...(typeof t.connection === "object" && t.connection !== null ? t.connection : {}),
            ...(Array.isArray(t.points) && t.points.length > 0 ? { points: t.points } : {}),
        }));
    }
    return out;
}

// ── 会话历史持久化（跨轮完整上下文）───────────────────────
// 前端回传的 history 只含 user/assistant 纯文本，工具调用/结果证据丢失，
// 导致模型在后续轮看不到已解析的点表等工具产出、陷入自我怀疑死循环。
// 服务端按 conversationId 保存完整消息历史（含工具调用/结果），后续轮优先恢复。

/** 跨轮持久化的消息形状——与 LangChain coerceMessageLikeToMessage 兼容的普通字典 */
interface HistoryMsg {
    role: "user" | "assistant" | "system" | "tool";
    content: string;
    tool_calls?: Array<{ name: string; args: unknown; id: string; type: "tool_call" }>;
    tool_call_id?: string;
    name?: string;
}

/** 历史上限——防止长会话下内存/上下文无界增长 */
const MAX_HISTORY_MSGS = 100;

/** BaseMessage.content 可能是 string 或 content blocks，统一转为字符串 */
function contentToString(content: unknown): string {
    if (typeof content === "string") {
        return content;
    }
    if (Array.isArray(content)) {
        return content
            .map((b) => {
                const block = b as { text?: unknown; type?: unknown };
                return block && typeof block.text === "string" ? block.text : "";
            })
            .join("");
    }
    return "";
}

/** 将 LangChain 消息序列化为可跨轮存储的普通字典；无法识别的类型返回 null */
function toHistoryMsg(m: BaseMessage): HistoryMsg | null {
    if (isHumanMessage(m)) {
        return { role: "user", content: contentToString(m.content) };
    }
    if (isSystemMessage(m)) {
        return { role: "system", content: contentToString(m.content) };
    }
    if (isAIMessage(m)) {
        const toolCalls = (m.tool_calls ?? []).map((tc) => ({
            name: tc.name,
            args: tc.args,
            id: typeof tc.id === "string" ? tc.id : "",
            type: "tool_call" as const,
        }));
        return {
            role: "assistant",
            content: contentToString(m.content),
            ...(toolCalls.length > 0 ? { tool_calls: toolCalls } : {}),
        };
    }
    if (isToolMessage(m)) {
        return {
            role: "tool",
            content: contentToString(m.content),
            tool_call_id: m.tool_call_id,
            name: m.name ?? "",
        };
    }
    return null;
}

/** 抖动兜底注入的 nudge 消息内容——持久化历史时剔除，避免污染跨轮上下文 */
const NUDGE_CONTENTS = new Set<string>([
    "请用中文向用户总结以上工具的执行结果。",
    "请立即调用你刚才宣布要调用的工具继续完成任务，不要仅用文字说明。",
    "展示接入方案前必须先调用 output_access_plan 工具产出结构化方案。请立即调用，然后再展示摘要。",
    "你必须立即调用 output_plan_steps 工具。不要用文字回答。",
]);

export async function createC4Agent(
    config: SuperWorkerConfig,
): Promise<C4Agent> {
    const agent = await createSuperWorker(config);

    // 跨轮持久化的设备信息与接入方案——"生成方案"轮产出，"确认"轮注入，
    // 避免依赖 LLM 从历史重新推导（追加设备/修改/删除场景易出错）。
    let deviceInfo: Record<string, unknown> | null = null;
    let accessPlan: Record<string, unknown> | null = null;
    // 用户显式指定的数据接收端口——确定性捕获自用户消息（同场站名解析机制），
    // 确认轮注入，防止 LLM 重试时回退 registry 默认端口
    let userPort: string | null = null;
    // 执行闸门状态（agent.md「执行闸门」）：是否收到过用户确认
    let userConfirmed = false;
    // 会话历史（含工具调用/结果证据）——按 conversationId 持久化，跨轮恢复完整上下文
    const conversationHistories = new Map<string, HistoryMsg[]>();

    return {
        invoke: async function* (input) {
            const conversation = input.conversationId ?? "unknown";
            const log = config.agentLogger;
            // 恢复服务端历史：后续轮优先使用服务端完整历史（含工具证据），
            // 前端 text-only history 仅作服务端无记录时的兜底
            const storedHistory = conversationHistories.get(conversation);
            if (storedHistory && storedHistory.length > 0 && input.messages.length > 0) {
                const newMsg = input.messages[input.messages.length - 1];
                input.messages = [...storedHistory, newMsg];
            }
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
                    // 确定性捕获用户显式指定的接收端口（如「接收端口使用7867」），
                    // 供确认轮注入——LLM 从提示词默认值回退的教训见 func_test_case 用例 5。
                    // 必须带「接收/监听」前缀：裸「端口」会误捕转发端口（如「转发目标端口9999」）
                    if (last.role === "user") {
                        const portMatch = lastContent.match(
                            /(?:接收|监听)端口[^0-9]{0,6}([0-9]{4,5})/,
                        );
                        if (portMatch) {
                            userPort = portMatch[1];
                        }
                        // 确定性捕获用户显式声明的协议（如「使用asfp2协议」）——
                        // 防止重新解析时重新推断协议（func_test_case 用例 11）
                        const protoMatch = lastContent.match(
                            /\b(asfp2|modbus(?:\s*tcp)?|iec104|influxdb)\b/i,
                        );
                        if (protoMatch) {
                            config.declared_protocol = normalize_protocol(
                                protoMatch[1].toLowerCase().replace(/\s+/g, "_"),
                            );
                        }
                    }
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
                    // 按钮确认是唯一确认通道：仅前端确认按钮发送的结构化消息
                    // （前缀 [C4_BUTTON_CONFIRM]，见 frontend useConfirmDetect.ts）置位
                    // userConfirmed；自由文本中的确认词不构成确认
                    if (
                        last.role === "user" &&
                        !isReject &&
                        lastContent.startsWith("[C4_BUTTON_CONFIRM]")
                    ) {
                        confirmOriginalContent = lastContent;
                        isConfirm = true;
                        userConfirmed = true;
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
                        if (userPort) {
                            extra.push(
                                `用户指定的数据接收端口: ${userPort}（必须原样使用，禁止改为默认端口）`,
                            );
                        }
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

                    // 自由文本确认引导（确定性，不经 LLM）：确认按钮是唯一确认通道，
                    // 短确认词消息不构成确认——固定引导用户点击按钮，防止模型宣而不行
                    // 后静默无操作（func_test_case 用例 11）。长消息中的「确认」字样
                    // （如「确认修改端口为 9001」）走正常流程，不受影响
                    if (
                        last.role === "user" &&
                        !isReject &&
                        !lastContent.startsWith("[C4_BUTTON") &&
                        lastContent.replace(/\s/g, "").length <= 6 &&
                        /确认|执行/.test(lastContent)
                    ) {
                        yield {
                            type: "text" as const,
                            content:
                                "⚠️ 接入尚未执行——请点击下方「确认」按钮以执行接入（文字回复不作为确认依据）。是否确认执行？",
                        };
                        return;
                    }
                }

                const MAX_MISSES = 3;
                let misses = 0;
                // 空回复抖动兜底：工具执行后模型可能零文本输出（解析结果不展示、
                // 对话历史断链，func_test_case 用例 9）——限补跑一次总结
                let summaryNudgeUsed = false;
                // 「宣而不行」抖动兜底：模型以纯文本宣布下一步工具（如「让我先输出
                // 结构化设备信息」）却未实际调用，图收尾后流程停滞——限补跑一次
                let announceNudgeUsed = false;
                // 方案展示绕过工具兜底：模型不经 output_access_plan 直接展示方案并索要
                // 确认（按钮未武装，用户无确认手段，func_test_case 用例 11）——限一次
                let accessPlanForceUsed = false;
                // 最后一轮 streamEvents 的 run 流——用于在收尾时读取最终状态并持久化历史
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                let lastStream: any = null;

                while (misses <= MAX_MISSES) {
                    let hasToolCall = false;
                    let producedText = false;
                    let lastTextSample = "";
                    let lastMsgHadToolCall = false;

                    log?.llm_call(conversation, misses + 1, input.messages);

                    const stream = await (agent as any).streamEvents(
                        { messages: input.messages },
                        { version: "v3", configurable: { recursion_limit: 50 } },
                    );
                    lastStream = stream;

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
                                    // 新方案产生 → 旧确认失效，须重新点击确认按钮（执行闸门）
                                    userConfirmed = false;
                                    userPort = null;
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
                            } catch (bg_err) {
                                // 工具执行异常不再静默：不可见的失败会让模型宣而不行后
                                // 无法定位（func_test_case 用例 11）
                                log?.tool_result(conversation, "tool_exec_error", JSON.stringify({
                                    error: bg_err instanceof Error ? bg_err.message : String(bg_err),
                                }));
                            }
                        }
                    })();

                    for await (const msg of stream.messages) {
                        const textParts: string[] = [];
                        lastMsgHadToolCall = false;
                        for await (const token of msg.text) {
                            textParts.push(token);
                            yield { type: "text" as const, content: token };
                        }
                        if (textParts.length > 0) {
                            producedText = true;
                            lastTextSample = textParts.join("");
                            log?.llm_text(conversation, lastTextSample);
                        }
                        for await (const tc of msg.toolCalls) {
                            const tcArgs = (tc as unknown as { args?: unknown }).args ?? {};
                            yield { type: "tool_call" as const, name: tc.name, args: {} };
                            log?.tool_call(conversation, tc.name, tcArgs);
                            hasToolCall = true;
                            lastMsgHadToolCall = true;
                        }
                    }

                    await bgCapture;
                    for (const tr of toolResults) {
                        yield { type: "tool_result" as const, name: tr.name, result: tr.result };
                    }

                    if (hasToolCall && !producedText && !summaryNudgeUsed) {
                        summaryNudgeUsed = true;
                        misses++;
                        input.messages = [...input.messages, {
                            role: "user" as const,
                            content: "请用中文向用户总结以上工具的执行结果。",
                        }];
                        continue;
                    }

                    // 「宣而不行」抖动兜底：末条消息是纯文本且宣布了下一步动作
                    // （「让我先输出/调用/解析…」）却未实际调用（含整轮零工具调用的
                    // 长推理摇摆，func_test_case 用例 11 复发）——注入指令补跑一轮
                    // （限一次）；纯总结性收尾与向用户提问（无宣布动词）不受影响
                    const announced_unacted =
                        producedText &&
                        !lastMsgHadToolCall &&
                        /让我(?:先)?(?:输出|调用|执行|解析|检查)/.test(lastTextSample);
                    if (announced_unacted && !announceNudgeUsed) {
                        announceNudgeUsed = true;
                        misses++;
                        input.messages = [...input.messages, {
                            role: "user" as const,
                            content: "请立即调用你刚才宣布要调用的工具继续完成任务，不要仅用文字说明。",
                        }];
                        continue;
                    }

                    // 方案展示绕过工具兜底：本轮有方案展示文本（含确认句式）但未调用
                    // output_access_plan——注入强制其先产出结构化方案（限一次），
                    // planArmed 随之武装，确认按钮才会出现
                    const plan_presented =
                        producedText &&
                        /是否确认|确认执行|请确认/.test(lastTextSample);
                    const access_plan_ran = toolResults.some(
                        (tr) => tr.name === "output_access_plan",
                    );
                    if (plan_presented && !access_plan_ran && !accessPlanForceUsed) {
                        accessPlanForceUsed = true;
                        misses++;
                        input.messages = [...input.messages, {
                            role: "user" as const,
                            content: "展示接入方案前必须先调用 output_access_plan 工具产出结构化方案。请立即调用，然后再展示摘要。",
                        }];
                        continue;
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
                    // planDeviceInfo（output_device_info 输出）即 plan_steps 输入形状，优先；
                    // planAccessPlan（output_access_plan 输出）为 connection 嵌套形状，需适配展开
                    const pdi = planDeviceInfo as Record<string, unknown> | null;
                    const pdi_devices = pdi?.devices as unknown[] | undefined;
                    const adapted =
                        planAccessPlan && !(pdi_devices && pdi_devices.length > 0)
                            ? access_plan_to_steps_input(planAccessPlan)
                            : null;
                    let data: any = pdi_devices && pdi_devices.length > 0 ? pdi : adapted;
                    // 补齐：deviceInfo 抽取可能早于用户提供端口等实例字段（字段缺失），
                    // 用 accessPlan 同名设备的连接字段补齐（accessPlan 已过方案层必填校验）。
                    // 不补齐时兜底重生成会缺 port 失败，把修复拖入下一轮（用例 9 教训）
                    if (data === pdi && adapted && Array.isArray(adapted.devices)) {
                        const accByName = new Map<string, Record<string, unknown>>();
                        for (const d of adapted.devices as Array<Record<string, unknown>>) {
                            if (typeof d.name === "string") accByName.set(d.name, d);
                        }
                        for (const dev of (data.devices as Array<Record<string, unknown>>) ?? []) {
                            const acc = typeof dev.name === "string" ? accByName.get(dev.name) : undefined;
                            if (!acc) continue;
                            for (const [k, v] of Object.entries(acc)) {
                                if (k === "name" || k === "points" || k === "abbr") continue;
                                if (dev[k] === undefined || dev[k] === null || dev[k] === "") {
                                    dev[k] = v;
                                }
                            }
                        }
                        if (!data.site && adapted.site) {
                            data.site = adapted.site;
                        }
                    }
                    // N1 修复：forward_targets 双源合并——output_device_info 只回显 devices，
                    // 若确认轮数据缺失转发目标，会导致重生成静默丢弃用户要求的转发。
                    // ① pdi 的 forward_targets 优先（含 info-gatherer 固化的 abbr 候选）；
                    // ② 其次 accessPlan 适配出的（已过方案层 registry 必填校验）
                    if (data && (!Array.isArray(data.forward_targets) || data.forward_targets.length === 0)) {
                        const pdi_ft = (pdi as Record<string, unknown> | null)?.forward_targets as
                            | unknown[]
                            | undefined;
                        const adapted_ft = adapted?.forward_targets as unknown[] | undefined;
                        const ft = pdi_ft && pdi_ft.length > 0 ? pdi_ft : adapted_ft;
                        if (ft && ft.length > 0) {
                            data = { ...data, forward_targets: ft };
                        }
                    }
                    if (!data?.devices && confirmOriginalContent) {
                        const jsonMatch = confirmOriginalContent.match(/\{[\s\S]*"devices"[\s\S]*\}/);
                        if (jsonMatch) {
                            try { data = JSON.parse(jsonMatch[0]); } catch { /* ignore */ }
                        }
                    }
                    // 兜底重生成仅在 LLM 完全未产出计划时进行（planSteps 为 null）：
                    // LLM 已产出并经工具校验的计划不得被覆盖——pdi 缺转发目标时重生成
                    // 只产出采集单步，会静默丢弃转发（func_test_case 用例 9 复发根因）
                    if (data?.devices && !planSteps) {
                        try {
                            const genTool = createOutputPlanStepsTool(config.registry, config.site);
                            const raw = await (genTool as any).invoke(data);
                            const r = typeof raw === "string" ? JSON.parse(raw) : raw;
                            log?.tool_result(
                                conversation,
                                "confirm_regen",
                                JSON.stringify({
                                    source: pdi_devices && pdi_devices.length > 0 ? "planDeviceInfo" : "accessPlan",
                                    regen_success: Boolean(r?.success),
                                    steps_count: Array.isArray(r?.steps) ? r.steps.length : 0,
                                    error: r?.error ?? null,
                                }),
                            );
                            if (r?.success && Array.isArray(r?.steps)) {
                                planSteps = r.steps as ServiceStep[];
                            }
                        } catch (regen_err) {
                            log?.tool_result(conversation, "confirm_regen", JSON.stringify({
                                regen_success: false,
                                error: regen_err instanceof Error ? regen_err.message : String(regen_err),
                            }));
                        }
                    }
                }

                if (planSteps && planSteps.length > 0) {
                    log?.tool_result(
                        conversation,
                        "plan_steps_final",
                        JSON.stringify(
                            planSteps.map((s) => ({
                                action: s.action,
                                service_type: s.service_type,
                                id: (s.instance as Record<string, unknown>)?.id,
                                points: Array.isArray(s.points) ? s.points.length : 0,
                            })),
                        ),
                    );
                    if (!userConfirmed) {
                        log?.error(conversation, "执行被拒绝: 未收到确认按钮消息");
                        // 以 text 进入对话历史（error 气泡不入 history，LLM 下轮将无从得知
                        // 执行被拒，会误报「执行完成」）；句式含「是否确认执行」使前端按钮重现
                        yield {
                            type: "text" as const,
                            content:
                                "⚠️ 本次接入未执行，配置未写入。请点击下方「确认」按钮完成确认（文字回复不作为确认依据）。是否确认执行？",
                        };
                        planSteps = null;
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
                                const translator = buildErrorTranslator(config.registry);
                                const reasons = ssr.failed_services
                                    .map((f) => `${f.service_type}: ${translateError(f.error ?? "", translator)}`)
                                    .join("；");
                                const portHint =
                                    /PORT_BIND_FAILED|PORT_CONFLICT/.test(details)
                                        ? "如需更换端口，请删除该设备后重新接入并指定新的端口。"
                                        : "";
                                yield {
                                    type: "error" as const,
                                    message: `部分服务启动失败: ${names}（${reasons}）${portHint}`,
                                };
                            }
                            config.state?.setPhase("idle");
                            log?.phase(conversation, "idle");
                            config.state?.setAccessPlan(false);
                            // 确认已随本次执行消耗，复位闸门（下一条新接入须重新确认）
                            userConfirmed = false;
                            userPort = null;
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
                // 闸门状态不复位于此：userConfirmed/userPort 在执行完成后才复位——
                // 「执行失败 → 补问 → 重试」的多轮修复必须跨轮保持确认（func_test_case 用例 9）

                // 持久化本轮完整历史（含工具调用/结果证据），供后续轮恢复上下文
                try {
                    const finalState = await lastStream?.output;
                    const msgs = (finalState?.messages ?? []) as BaseMessage[];
                    const clean = msgs
                        .map(toHistoryMsg)
                        .filter((m): m is HistoryMsg => m !== null)
                        .filter(
                            (m) => !(m.role === "user" && NUDGE_CONTENTS.has(m.content)),
                        );
                    if (clean.length > MAX_HISTORY_MSGS) {
                        conversationHistories.set(
                            conversation,
                            clean.slice(clean.length - MAX_HISTORY_MSGS),
                        );
                    } else if (clean.length > 0) {
                        conversationHistories.set(conversation, clean);
                    }
                } catch {
                    // 历史持久化失败不影响主流程（下一轮退化到前端 text-only history）
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
