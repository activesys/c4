// c4/agent/frontend/src/api/upload.ts
// POST /api/upload — web.md §3.2.
//
// Multipart upload of a single file. The backend accepts a broad extension
// whitelist but only `.xlsx` / `.csv` / `.xls` / `.txt` actually have parsers
// wired up (`.pdf`, `.docx`, images: multer lets them through but the agent
// has no extraction tool). The frontend uses `classifyFileType` to mark the
// unsupported bucket as "暂不支持解析" before the user wastes a round-trip.
//
// Stream semantics are identical to /api/chat: ReadableStream exhaustion is
// a valid terminal, even when no `event: done` is sent (web.md §4.2).

import { sseParser, type SseEvent } from "./sse";

const PARSEABLE_EXTENSIONS = new Set([".xlsx", ".csv", ".xls", ".txt"]);

/** Extract a lowercase extension from a filename (".pdf" from "REPORT.PDF"). */
function extOf(filename: string): string {
  const idx = filename.lastIndexOf(".");
  return idx >= 0 ? filename.slice(idx).toLowerCase() : "";
}

/**
 * Classify a filename by extension. Returns "parseable" for formats the
 * backend can actually parse (.xlsx/.csv/.xls/.txt) and "unsupported" for
 * everything else the whitelist allows but the agent cannot extract
 * (.pdf/.docx/.doc and image formats).
 */
export function classifyFileType(
  filename: string,
): "parseable" | "unsupported" {
  return PARSEABLE_EXTENSIONS.has(extOf(filename)) ? "parseable" : "unsupported";
}

export interface UploadOptions {
  /** Optional text message to attach to the upload form. */
  message?: string;
  signal?: AbortSignal;
}

export interface UploadParams {
  file: File;
  message?: string;
  /** 当前会话 ID——上传解析轮与后续对话轮须同属一个会话，服务端才能恢复跨轮工具证据 */
  conversationId?: string;
}

export type UploadEventHandler = (event: SseEvent) => void;

/**
 * POST /api/upload and stream SSE events back. Resolves with the echoed
 * conversationId (X-Conversation-Id header) once the stream ends (either via
 * `event: done` or ReadableStream exhaustion), so the caller can continue the
 * same conversation on subsequent `/api/chat` turns.
 */
export async function streamUpload(
  params: UploadParams,
  onEvent: UploadEventHandler,
  opts: UploadOptions = {},
): Promise<string> {
  const form = new FormData();
  form.append("file", params.file);
  if (params.message) form.append("message", params.message);
  if (params.conversationId) form.append("conversationId", params.conversationId);

  const res = await fetch("/api/upload", {
    method: "POST",
    body: form,
    signal: opts.signal,
  });

  if (!res.ok) {
    throw new Error(`文件上传失败: HTTP ${res.status}`);
  }
  if (!res.body) {
    throw new Error("文件上传失败: 响应为空");
  }

  const conversationId =
    res.headers.get("X-Conversation-Id") ?? params.conversationId ?? "";

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    if (parts.length === 0) continue;

    const chunk = parts.join("\n\n") + "\n\n";
    for (const ev of sseParser(chunk)) {
      onEvent(ev);
      if (ev.type === "done" || ev.type === "error") {
        try {
          await reader.cancel();
        } catch {
          // ignore double-cancel
        }
        return conversationId;
      }
    }
  }

  // Stream close fallback — same as streamChat.
  if (buffer.trim().length > 0) {
    for (const ev of sseParser(buffer + "\n\n")) onEvent(ev);
  }
  return conversationId;
}
