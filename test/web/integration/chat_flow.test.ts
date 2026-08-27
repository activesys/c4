// @vitest-environment node
// L2 对话流集成测试 — web.md §3.1.2（用例 4.1.1 / 4.1.2 / 4.1.3）

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { streamChat, type ChatMessage } from "@frontend/api/chat";
import type { SseEvent } from "@frontend/api/sse";
import { startAgent, type AgentHandle } from "./fixtures";

let agent: AgentHandle;

beforeAll(async () => {
  agent = await startAgent();
}, 120_000);

afterAll(async () => {
  await agent.stop();
}, 30_000);

describe.skipIf(!process.env.DEEPSEEK_API_KEY)("对话流（真实后端 + LLM）", () => {
  it("4.1.1 流式渲染真实对话：POST「你好」→ 文本气泡、无 error、流关闭", async () => {
    const events: SseEvent[] = [];
    const echoed = await streamChat({ message: "你好" }, (ev) => events.push(ev));

    expect(events.length).toBeGreaterThan(0);

    const textEvents = events.filter((e) => e.type === "text");
    expect(textEvents.length).toBeGreaterThan(0);

    const joined = textEvents
      .map((e) => String(e.data.content ?? ""))
      .join("")
      .trim();
    expect(joined.length).toBeGreaterThan(0);

    expect(events.some((e) => e.type === "error")).toBe(false);

    // streamChat 已 resolve，说明流已按 done 或流关闭兜底终止（web.md §4.2）
    expect(echoed).toBeDefined();
  });

  it("4.1.2 conversationId 回显：X-Conversation-Id 头 + 事件均回显 c123", async () => {
    const res = await fetch(`${agent.baseUrl}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "你好", conversationId: "c123" }),
    });
    expect(res.headers.get("X-Conversation-Id")).toBe("c123");
    await res.body?.cancel();

    const events: SseEvent[] = [];
    const echoed = await streamChat(
      { message: "你好", conversationId: "c123" },
      (ev) => events.push(ev),
    );
    expect(echoed).toBe("c123");

    const echoEvents = events.filter(
      (e) => e.data && typeof e.data.conversationId === "string",
    );
    expect(echoEvents.length).toBeGreaterThan(0);
    for (const ev of echoEvents) {
      expect(ev.data.conversationId).toBe("c123");
    }
  });

  it("4.1.3 history 回传：带 history 的正常响应", async () => {
    const history: ChatMessage[] = [
      { role: "user", content: "你好" },
      { role: "assistant", content: "你好，有什么可以帮您？" },
    ];
    const events: SseEvent[] = [];
    await streamChat(
      { message: "请继续", history },
      (ev) => events.push(ev),
    );

    const textEvents = events.filter((e) => e.type === "text");
    expect(textEvents.length).toBeGreaterThan(0);
    expect(events.some((e) => e.type === "error")).toBe(false);
  });
});
