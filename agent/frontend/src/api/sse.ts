// c4/agent/frontend/src/api/sse.ts
// Pure SSE chunk parser — web.md §3.1.1, §4.2.
//
// SSE framing (RFC):
//   - Lines starting with `:` are comments (e.g. `:ok` keepalive) — ignored.
//   - `event: <name>` sets the named event for the current record.
//   - `data: <text>` (one or more lines) is the payload; we join multiple data:
//     lines with `\n`, then parse JSON.
//   - A blank line ends a record and yields one SseEvent.
//
// Rules per web.md §3.1.1:
//   - Named `event: X` records → type = X (e.g. "done", "error", "interrupt").
//   - Default (no event: line) records → type = data.type if present, else "message".
//   - The `event` field of SseEvent exposes the raw named event (null for default).
//
// This is a pure function: chunk in, SseEvent[] out. Stream-level concerns
// (fetch loop, "stream close" fallback) live in api/chat.ts.

export type SSEEventType =
  | "text"
  | "tool_call"
  | "tool_result"
  | "done"
  | "error"
  | "interrupt"
  | "message";

export interface SseEvent {
  /** Logical event type — what consumers dispatch on. */
  type: SSEEventType;
  /** The raw named event from the `event:` line, or null for default `data:` records. */
  event: string | null;
  /** Parsed JSON payload from `data:` lines. */
  data: Record<string, unknown>;
}

export function sseParser(text: string): SseEvent[] {
  const events: SseEvent[] = [];

  // Normalize CRLF → LF so the same splitter works on either wire format.
  const normalized = text.replace(/\r\n/g, "\n");

  // Records are separated by blank lines (`\n\n`). A trailing newline that does
  // not introduce another blank line is fine — it produces no extra record.
  const records = normalized.split("\n\n");

  for (const record of records) {
    if (record === "") continue;

    let eventName: string | null = null;
    const dataLines: string[] = [];

    // Split the record into lines. We deliberately do NOT trim — leading
    // spaces in SSE field lines are significant (they mean "line continuation").
    const lines = record.split("\n");
    for (const line of lines) {
      if (line === "") continue;
      if (line.startsWith(":")) {
        // Comment — ignore (web.md §3.1.1 注意项)
        continue;
      }
      if (line.startsWith("event:")) {
        eventName = line.slice("event:".length).trim();
        continue;
      }
      if (line.startsWith("data:")) {
        // Strip the leading "data:" and the optional single space (SSE convention).
        let payload = line.slice("data:".length);
        if (payload.startsWith(" ")) payload = payload.slice(1);
        dataLines.push(payload);
      }
      // Other fields (id:, retry:) are not used by the backend — ignored.
    }

    if (dataLines.length === 0) continue;

    const rawData = dataLines.join("\n");
    let parsed: Record<string, unknown>;
    try {
      const value = JSON.parse(rawData);
      // Defensive: a payload might be a JSON primitive. Wrap into an object so
      // the SseEvent.data shape stays consistent for consumers.
      parsed =
        value !== null && typeof value === "object" && !Array.isArray(value)
          ? (value as Record<string, unknown>)
          : { value };
    } catch {
      // Malformed JSON — surface as a message event with the raw text so the
      // consumer can at least see what came in instead of silently dropping it.
      parsed = { raw: rawData };
    }

    let type: SSEEventType;
    if (eventName !== null) {
      // Named event line wins — backend explicitly tags done/error/interrupt.
      type = eventName as SSEEventType;
    } else {
      const dataType = parsed.type;
      type =
        typeof dataType === "string"
          ? (dataType as SSEEventType)
          : "message";
    }

    events.push({ type, event: eventName, data: parsed });
  }

  return events;
}
