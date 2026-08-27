// @vitest-environment node
// L2 文件上传集成测试 — web.md §3.2（用例 4.2.1 / 4.2.2 / 4.2.3）

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { classifyFileType, streamUpload } from "@frontend/api/upload";
import type { SseEvent } from "@frontend/api/sse";
import { startAgent, type AgentHandle } from "./fixtures";

let agent: AgentHandle;

beforeAll(async () => {
  agent = await startAgent();
}, 120_000);

afterAll(async () => {
  await agent.stop();
}, 30_000);

function txtFile(name: string): File {
  return new File(["设备名称,寄存器地址,数据类型\n1#风机,40001,uint16\n"], name, {
    type: "text/plain",
  });
}

describe.skipIf(!process.env.DEEPSEEK_API_KEY)("文件上传（真实后端 + LLM）", () => {
  it("4.2.1 上传可解析 .txt → SSE 流含文本、流正常关闭", async () => {
    const events: SseEvent[] = [];
    await streamUpload(
      { file: txtFile("points.txt"), message: "请解析此文件中的设备信息" },
      (ev) => events.push(ev),
    );

    expect(events.length).toBeGreaterThan(0);

    const textEvents = events.filter((e) => e.type === "text");
    expect(textEvents.length).toBeGreaterThan(0);

    const joined = textEvents
      .map((e) => String(e.data.content ?? ""))
      .join("")
      .trim();
    expect(joined.length).toBeGreaterThan(0);
    expect(events.some((e) => e.type === "error")).toBe(false);
  });

  it("4.2.1 上传可解析 .csv → SSE 流含文本", async () => {
    const file = new File(["name,addr,type\n1#风机,40001,uint16\n"], "points.csv", {
      type: "text/csv",
    });
    const events: SseEvent[] = [];
    await streamUpload(
      { file, message: "请解析此文件中的设备信息" },
      (ev) => events.push(ev),
    );

    const textEvents = events.filter((e) => e.type === "text");
    expect(textEvents.length).toBeGreaterThan(0);
  });
});

describe("文件上传（确定性契约）", () => {
  it("4.2.2 upload 事件不含 conversationId", async () => {
    const events: SseEvent[] = [];
    await streamUpload(
      { file: txtFile("points2.txt"), message: "请解析此文件中的设备信息" },
      (ev) => events.push(ev),
    );

    expect(events.length).toBeGreaterThan(0);
    for (const ev of events) {
      expect(ev.data.conversationId).toBeUndefined();
    }
  });

  it("4.2.3 classifyFileType 格式判定", () => {
    expect(classifyFileType("points.xlsx")).toBe("parseable");
    expect(classifyFileType("points.csv")).toBe("parseable");
    expect(classifyFileType("points.txt")).toBe("parseable");

    expect(classifyFileType("report.pdf")).toBe("unsupported");
    expect(classifyFileType("report.docx")).toBe("unsupported");
    expect(classifyFileType("photo.png")).toBe("unsupported");
    expect(classifyFileType("photo.jpg")).toBe("unsupported");
  });
});
