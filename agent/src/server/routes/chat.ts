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
    // Express 5 async handler 会缓冲 write，需要显式 flush
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if (typeof (res as any).flush === "function") {
        (res as any).flush();
    }
}

/** Write an SSE error and close the stream. */
function sendError(res: Response, message: string): void {
    sendSSE(res, "error", { message });
    res.end();
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
        const conversationId = body.conversationId ?? randomUUID();

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
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        if ((res as any).socket) {
            (res as any).socket.setNoDelay(true);
            (res as any).socket.setTimeout(0);
        }

        // 使用 promise chain 而非 async/for-await，规避 Express v5 在 async
        // handler 返回 pending Promise 时关闭连接的问题。
        const stream = agent.invoke({ messages });

        function processNext(
            result: IteratorResult<AgentStreamEvent>,
        ): void {
            if (result.done) {
                res.end();
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
            const message =
                err instanceof Error ? err.message : String(err);
            sendSSE(res, "error", { message, conversationId });
            res.end();
        }

        stream.next().then(processNext, handleError);
    });

    return router;
}
