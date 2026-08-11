// c4/agent/src/server/routes/upload.ts — POST /api/upload file upload + SSE
// Design: agent.md §3.5 — multer saves file to /tmp/, invokes agent with file path, streams SSE

import { Router, type Request, type Response } from "express";
import multer from "multer";
import { randomUUID } from "node:crypto";
import { extname } from "node:path";
import type { C4Agent, AgentStreamEvent } from "../types.js";

// ── Multer Configuration ──────────────────────────────────

/** Max file size: 50 MB */
const MAX_FILE_SIZE = 50 * 1024 * 1024;

/**
 * 允许的扩展名 → MIME 映射。
 * 因浏览器和测试客户端常用 application/octet-stream，
 * 此处以扩展名为准判定支持的格式。
 */
const ALLOWED_EXTENSIONS = new Set([
    ".xlsx", ".csv", ".xls",
    ".pdf", ".docx", ".doc",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".txt",
]);

const storage = multer.diskStorage({
    destination: (_req, _file, cb) => {
        cb(null, "/tmp");
    },
    filename: (_req, file, cb) => {
        const uniqueId = randomUUID();
        const ext = extname(file.originalname) || "";
        cb(null, `c4_upload_${uniqueId}${ext}`);
    },
});

const upload = multer({
    storage,
    limits: {
        fileSize: MAX_FILE_SIZE,
        files: 1,
    },
    fileFilter: (_req, file, cb) => {
        const ext = extname(file.originalname).toLowerCase();
        if (ALLOWED_EXTENSIONS.has(ext)) {
            cb(null, true);
        } else {
            cb(
                new Error(
                    `不支持的文件类型: ${ext || "未知"}。` +
                    "支持 .xlsx .csv .xls .pdf .docx .doc .png .jpg .gif .bmp",
                ),
            );
        }
    },
});

// ── SSE Helpers (same pattern as chat.ts) ─────────────────

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

// ── Router Factory ────────────────────────────────────────

/**
 * Create the upload router with the C4 Agent instance.
 *
 * @param agent - C4Agent instance (injected, like chat route)
 * @returns Express Router handling POST /api/upload
 */
export function createUploadRouter(agent: C4Agent): Router {
    const router = Router();

    router.post("/", (req: Request, res: Response) => {
        upload.single("file")(req, res, (err: unknown) => {
            if (err) {
                // Multer error — stream friendly SSE message to the client
                const message =
                    err instanceof multer.MulterError
                        ? `上传失败: ${err.message}`
                        : err instanceof Error
                            ? err.message
                            : "文件上传失败";

                res.setHeader("Content-Type", "text/event-stream");
                res.setHeader("Cache-Control", "no-cache");
                res.setHeader("Connection", "keep-alive");
                res.setHeader("X-Accel-Buffering", "no");
                res.flushHeaders();

                sendSSE(res, null, {
                    type: "text",
                    content: message,
                });
                res.end();
                return;
            }

            const file = req.file;
            if (!file) {
                res.status(400).json({
                    success: false,
                    error: "未收到文件。请使用 multipart/form-data，文件字段名为 'file'",
                });
                return;
            }

            // Build message: 传递文件路径 + 原始文件名
            const userMessage =
                typeof req.body.message === "string" && req.body.message.length > 0
                    ? req.body.message
                    : "请解析此文件中的设备信息";

            const prompt = [
                `用户上传了文件: path=${file.path}, name=${file.originalname}`,
                `用户消息: ${userMessage}`,
            ].join("\n");

            // Set SSE headers
            res.setHeader("Content-Type", "text/event-stream");
            res.setHeader("Cache-Control", "no-cache");
            res.setHeader("Connection", "keep-alive");
            res.setHeader("X-Accel-Buffering", "no");
            res.flushHeaders();

            // Keepalive
            res.write(":ok\n\n");
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            if ((res as any).socket) {
                (res as any).socket.setNoDelay(true);
                (res as any).socket.setTimeout(0);
            }

            const stream = agent.invoke({
                messages: [{ role: "user", content: prompt }],
            });

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
                        sendSSE(res, null, { type: "text", content: event.content });
                        break;
                    case "tool_call":
                        sendSSE(res, null, { type: "tool_call", name: event.name, args: event.args });
                        break;
                    case "tool_result":
                        sendSSE(res, null, { type: "tool_result", name: event.name, result: event.result });
                        break;
                    case "done":
                        sendSSE(res, "done", {});
                        break;
                    case "error":
                        sendSSE(res, "error", { message: event.message });
                        break;
                    default:
                        break;
                }

                stream.next().then(processNext, handleError);
            }

            function handleError(err: unknown): void {
                const message =
                    err instanceof Error ? err.message : String(err);
                sendSSE(res, "error", { message });
                res.end();
            }

            stream.next().then(processNext, handleError);
        });
    });

    return router;
}
