// c4/agent/src/orchestrator/display_orchestrator.ts — 数据展示 Agent 编排器
// 设计：agent.md §2.0（双 Agent 组）/§3.6.4（工具迁移注）——轻量回合循环
// 「意图 → 工具调用 → 回复」，无缺口流水线、无方案/确认/取消语义（终止不经对话
// 文本——双 ESC → POST /api/cancel，agent.md §2.4.2）。
// 工具面 = list_points + display_points 恰两个（stop_display 已删除）；LLM 只做
// 展示域窄提取（点名/频率/时长词形，display_prompt.txt），缺省应用与命中计算在
// DisplayService（确定性代码，§3.6.7）。展示 Agent 不写 phase（§2.0.3 状态面：
// phase 归接入编排器独占），历史按会话分桶（display:{cid} 键由 AgentGroup 归一）。

import { readFileSync } from "node:fs";
import * as path from "node:path";
import type { BaseMessage } from "@langchain/core/messages";
import { AIMessage, HumanMessage, SystemMessage, ToolMessage } from "@langchain/core/messages";
import type { StructuredTool } from "@langchain/core/tools";
import type { AgentLogger } from "../logging/agent_logger.js";
import type { AgentInvokeInput, AgentStreamEvent } from "../server/types.js";

export interface DisplayOrchestratorConfig {
    /** LLM 客户端（与接入编排器共享同一 createModel 产物，agent.md §2.0.1） */
    model: {
        bindTools?: (tools: StructuredTool[]) => unknown;
        invoke: (messages: BaseMessage[], config?: { signal?: AbortSignal }) => Promise<unknown>;
    };
    /** 控制面工具（createDisplayTools 产物——恰 list_points + display_points） */
    tools: StructuredTool[];
    agentLogger: AgentLogger;
    /**
     * 停止当前活跃显示会话（DisplayService.stop 注入）：取消发生在订阅回合
     * 「会话已建立之后」时，回合中止须级联停止本次建立的会话（§2.4.2 订阅
     * 窗口并存——cancel 路由在取消时刻可能早于会话建立，由中止路径补偿）
     */
    stopActiveDisplay?: () => void;
}

/** 展示域会话历史上限（条；超出裁旧——展示回合短，40 条覆盖约 20 轮问答） */
const HISTORY_CAP = 40;
/** 会话分桶上限（与接入草稿 LRU 同量级） */
const HISTORY_BUCKETS = 16;
/** 单回合工具循环上限（防 LLM 空转；正常路径 1~3 次） */
const MAX_TOOL_ROUNDS = 5;

interface DisplayHistory {
    messages: BaseMessage[];
}

function render_system_prompt(): string {
    return readFileSync(
        path.join(
            path.dirname(new URL(import.meta.url).pathname),
            "..",
            "prompts",
            "display_prompt.txt",
        ),
        "utf-8",
    );
}

function text_of(message: unknown): string {
    if (message instanceof AIMessage) {
        const c = message.content;
        return typeof c === "string" ? c : "";
    }
    return "";
}

/**
 * 历史重放前净化：剔除孤儿 ToolMessage（其前一条 AIMessage 不含匹配 id 的
 * tool_calls）。模型偶发返回缺失 call id 的工具调用时，ToolMessage 的
 * tool_call_id 与 assistant 消息对不上，重放会被 OpenAI 兼容 API 以
 * 「role 'tool' must be a response to a preceding message with 'tool_calls'」
 * 400 拒绝，且该会话桶此后每轮都被同一历史打死——净化使重放始终合法。
 */
function sanitizeHistory(messages: BaseMessage[]): BaseMessage[] {
    const out: BaseMessage[] = [];
    let pending: Set<string> | null = null;
    for (const m of messages) {
        if (m instanceof AIMessage) {
            out.push(m);
            const ids = (m.tool_calls ?? []).map((c) => String(c.id ?? ""));
            pending = ids.length > 0 ? new Set(ids) : null;
        } else if (m instanceof ToolMessage) {
            const id = String(m.tool_call_id ?? "");
            if (pending !== null && id !== "" && pending.has(id)) {
                out.push(m);
            }
            // 孤儿（含空 id）——丢弃，不进入重放
        } else {
            out.push(m);
            pending = null;
        }
    }
    return out;
}

/**
 * 创建数据展示编排器——轻量「意图 → 工具调用 → 回复」循环（agent.md §3.6.4）。
 * 仅 invoke（无 rebindSite：展示 Agent 不持场站绑定）。
 */
export function createDisplayOrchestrator(cfg: DisplayOrchestratorConfig): {
    invoke(input: AgentInvokeInput): AsyncGenerator<AgentStreamEvent>;
} {
    const { model, tools, agentLogger, stopActiveDisplay } = cfg;
    const histories = new Map<string, DisplayHistory>();

    function history_of(conversationId: string | undefined): DisplayHistory {
        const key = conversationId ?? "display";
        let hit = histories.get(key);
        if (!hit) {
            hit = { messages: [] };
            histories.set(key, hit);
            if (histories.size > HISTORY_BUCKETS) {
                const oldest = histories.keys().next().value as string;
                histories.delete(oldest);
            }
        }
        return hit;
    }

    const bound = model.bindTools
        ? (model.bindTools(tools) as {
              invoke: (
                  messages: BaseMessage[],
                  config?: { signal?: AbortSignal },
              ) => Promise<AIMessage>;
          })
        : (model as unknown as {
              invoke: (
                  messages: BaseMessage[],
                  config?: { signal?: AbortSignal },
              ) => Promise<AIMessage>;
          });

    async function* invoke(
        input: AgentInvokeInput,
    ): AsyncGenerator<AgentStreamEvent> {
        const conversation = input.conversationId ?? "display";
        const userMsg = input.messages.filter((m) => m.role === "user").pop();
        const userText = userMsg ? userMsg.content : "";
        agentLogger.user_input(conversation, "user", userText);

        const history = history_of(input.conversationId);
        history.messages.push(new HumanMessage(userText));

        // 本回合是否成功建立了显示会话（中止级联停显的判定，§2.4.2）
        let createdDisplay = false;
        try {
            const system = render_system_prompt();
            let finalText = "";
            for (let round = 0; round < MAX_TOOL_ROUNDS; round++) {
                const ai = await bound.invoke(
                    [new SystemMessage(system), ...sanitizeHistory(history.messages)],
                    { signal: input.signal },
                );
                // 缺失 call id 的工具调用：就地回写合成 id——保证 ToolMessage 与
                // assistant 消息的 tool_calls 数组始终一致（重放配对前提）
                const calls = ai.tool_calls ?? [];
                calls.forEach((call, idx) => {
                    if (!call.id) {
                        call.id = `call_d${round}_${idx}_${Date.now() % 100000}`;
                    }
                });
                history.messages.push(ai);
                if (calls.length === 0) {
                    finalText = text_of(ai);
                    break;
                }
                for (const call of calls) {
                    const tool = tools.find((t) => t.name === call.name);
                    yield {
                        type: "tool_call",
                        name: call.name,
                        args: (call.args ?? {}) as Record<string, unknown>,
                    };
                    let resultText: string;
                    if (!tool) {
                        resultText = JSON.stringify({ error: `未知工具: ${call.name}` });
                    } else {
                        try {
                            resultText = await tool.invoke(
                                (call.args ?? {}) as Record<string, unknown>,
                            );
                        } catch (err) {
                            const msg = err instanceof Error ? err.message : String(err);
                            resultText = JSON.stringify({ error: msg });
                        }
                    }
                    if (call.name === "display_points" && resultText.includes('"ok":true')) {
                        createdDisplay = true;
                    }
                    history.messages.push(
                        new ToolMessage({
                            tool_call_id: call.id ?? "",
                            content: resultText,
                        }),
                    );
                    yield { type: "tool_result", name: call.name, result: resultText };
                }
            }
            if (finalText === "") {
                finalText = "显示请求已处理。";
            }
            if (history.messages.length > HISTORY_CAP) {
                history.messages = history.messages.slice(-HISTORY_CAP);
            }
            yield { type: "text", content: finalText };
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            agentLogger.error(conversation, `展示编排器异常: ${msg}`);
            yield { type: "error", message: `展示服务暂时不可用: ${msg}` };
        } finally {
            // 取消竞态补偿：cancel 先于会话建立到达时，回合在工具执行后才中止——
            // 本回合建立的显示会话须随之停止（否则活跃表格脱离取消语义存活）；
            // 仅停本回合建立者，不触碰其他会话的活跃显示
            if (input.signal?.aborted && createdDisplay) {
                try {
                    stopActiveDisplay?.();
                } catch {
                    // 停止失败不掩盖原异常
                }
            }
        }
        yield { type: "done" };
    }

    return { invoke };
}
