// c4/agent/src/server/routes/chat.ts — POST /api/chat SSE streaming
// Design: agent.md §3.5 (Web layer), LangServe-compatible SSE
//
// Accepts { message: string } JSON, streams agent.invoke() output as SSE.
// Supports interrupts — if the agent yields an `interrupt` event, the route
// waits for the client to resume with a follow-up POST containing
// { message, resume: true, interruptId }.

import { Router, type Request, type Response } from "express";
import type { C4Agent, AgentStreamEvent } from "../types.js";
import { randomUUID } from "node:crypto";

// ── Request body types ────────────────────────────────────
interface ChatRequestBody {
    /** User message text */
    message: string;
    /** Resume a pending interrupt */
    resume?: boolean;
    /** Interrupt ID to resume (required when resume=true) */
    interruptId?: string;
    /** Conversation ID for multi-turn state */
    conversationId?: string;
    /** Previous message history (optional, server can reconstruct from state) */
    history?: Array<{ role: string; content: string }>;
}

// ── SSE Helpers ───────────────────────────────────────────

/** Write an SSE data event. */
function sendSSE(res: Response, event: string | null, data: object): void {
    if (event) {
        res.write(`event: ${event}\n`);
    }
    res.write(`data: ${JSON.stringify(data)}\n\n`);
    // Express 5 async handler 会缓冲 write，需要显式 flush（compression 中间件注入的方法）
    const flushable = res as Response & { flush?: () => void };
    if (typeof flushable.flush === "function") {
        flushable.flush();
    }
}

// ── Router Factory ────────────────────────────────────────
/**
 * Create the chat router with the C4 Agent instance.
 *
 * @param agent - C4Agent instance (injected, not global)
 * @returns Express Router handling POST /api/chat
 */
export function createChatRouter(agent: C4Agent): Router {
    const router = Router();

    /**
     * POST /api/chat
     *
     * Body: { message: string, resume?: boolean, interruptId?: string,
     *         conversationId?: string, history?: Array<{role, content}> }
     *
     * Response: SSE stream with events:
     *   - data: { type: "text", content: "..." }        — token/response text
     *   - data: { type: "tool_call", name: "...", args: {...} } — agent tool call
     *   - data: { type: "tool_result", name: "...", result: "..." } — tool result
     *   - event: interrupt  data: { message: "...", interruptId: "..." } — user confirmation needed
     *   - event: done  data: {}                        — stream complete
     *   - event: error  data: { message: "..." }        — error occurred
     */
    router.post("/", (req: Request, res: Response) => {
        const body = req.body as ChatRequestBody;

        // Validate request body — 允许空消息，交给 agent 处理
        if (typeof body.message !== "string") {
            res.status(400).json({
                error: "Invalid request: 'message' field is required and must be a string",
            });
            return;
        }

        // Determine conversation ID (new or resumed)
        // （2026-09-28 修复）前端首消息传空串 conversationId——`??` 不挡空串，
        // 会话键退化为 "" 且回传空串不被前端存储；上传轮再生成新 ID 时同会话
        // 草稿丢失（已捕获的端口等全部清零，用例7 实测）。改用 || 兜底空串
        const conversationId = body.conversationId || randomUUID();

        // Build messages for agent invocation
        const messages: Array<{ role: string; content: string }> = [];

        // Include prior history if provided (for multi-turn context)
        if (Array.isArray(body.history)) {
            messages.push(...body.history);
        }

        // Add current user message
        if (body.resume) {
            // Resume flow: append the user's confirmation response
            messages.push({ role: "user", content: body.message });
        } else {
            // New message
            messages.push({ role: "user", content: body.message });
        }

        // Set SSE headers (Express v5)
        res.setHeader("Content-Type", "text/event-stream");
        res.setHeader("Cache-Control", "no-cache");
        res.setHeader("Connection", "keep-alive");
        res.setHeader("X-Accel-Buffering", "no");  // nginx buffering off
        res.setHeader("X-Conversation-Id", conversationId);
        res.flushHeaders();

        // Keepalive comment to prevent client disconnect during stream init
        res.write(":ok\n\n");
        // Express v5 在 async handler 间隙会关闭连接，禁用 TCP Nagle + 超时
        if (res.socket) {
            res.socket.setNoDelay(true);
            res.socket.setTimeout(0);
        }

        // 使用 promise chain 而非 async/for-await，规避 Express v5 在 async
        // handler 返回 pending Promise 时关闭连接的问题。
        //
        // 客户端断开（关页/开新对话/前端 abort）→ 取消信号触发在途 LLM 调用
        // 立即中断 + 终止流拉取：dying 回合不再空耗模型配额、也不再产生
        // phase 写入把徽标改回「收集信息中」（2026-10-05 用户实测根因）
        const controller = new AbortController();
        let closed = false;
        const stream = agent.invoke({
            messages,
            conversationId,
            signal: controller.signal,
        });
        res.on("close", () => {
            closed = true;
            controller.abort();
            void stream.return(undefined as never).catch(() => undefined);
        });

        function processNext(
            result: IteratorResult<AgentStreamEvent>,
        ): void {
            // 断开后剩余事件不再下发（stream.return 已在 close 中触发）
            if (closed || result.done) {
                if (!closed) res.end();
                return;
            }

            const event = result.value;
            switch (event.type) {
                case "text":
                    sendSSE(res, null, {
                        type: "text",
                        content: event.content,
                        conversationId,
                    });
                    break;

                case "button_arm":
                    // §2.4.4 回合终结按钮判定（web.md §3.1.3 v0.2.0）
                    sendSSE(res, null, { type: "button_arm", conversationId });
                    break;

                case "button_disarm":
                    sendSSE(res, null, {
                        type: "button_disarm",
                        reason: event.reason,
                        conversationId,
                    });
                    break;

                case "tool_call":
                    sendSSE(res, null, {
                        type: "tool_call",
                        name: event.name,
                        args: event.args,
                        conversationId,
                    });
                    break;

                case "tool_result":
                    sendSSE(res, null, {
                        type: "tool_result",
                        name: event.name,
                        result: event.result,
                        conversationId,
                    });
                    break;

                case "interrupt":
                    // Agent needs user confirmation — send interrupt event
                    // The frontend (@langchain/react useStream) will detect
                    // this and show a confirmation UI.
                    sendSSE(res, "interrupt", {
                        message: event.message,
                        interruptId: event.interruptId,
                        conversationId,
                    });
                    // Keep connection open for the client to send resume
                    break;

                case "done":
                    sendSSE(res, "done", { conversationId });
                    break;

                case "error":
                    sendSSE(res, "error", {
                        message: event.message,
                        conversationId,
                    });
                    break;

                default:
                    // Unknown event type — pass through as generic data
                    sendSSE(res, null, { type: "unknown", data: event });
                    break;
            }

            stream.next().then(processNext, handleError);
        }

        function handleError(err: unknown): void {
            // 断开引发的取消错误不下发（连接已关）
            if (closed) return;
            const message =
                err instanceof Error ? err.message : String(err);
            sendSSE(res, "error", { message, conversationId });
            res.end();
        }

        stream.next().then(processNext, handleError);
    });

    return router;
}
