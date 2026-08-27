// @vitest-environment node
// L2 服务目录集成测试 — web.md §3.3（用例 4.3.1）

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { fetchServices } from "@frontend/api/services";
import { startAgent, type AgentHandle } from "./fixtures";

let agent: AgentHandle;

beforeAll(async () => {
  agent = await startAgent();
}, 120_000);

afterAll(async () => {
  await agent.stop();
}, 30_000);

describe("服务目录（真实后端）", () => {
  it("4.3.1 fetchServices 返回 5 个目录条目且字段完整", async () => {
    const services = await fetchServices();
    expect(services.length).toBe(5);

    const types = services.map((s) => s.service_type);
    expect(new Set(types).size).toBe(5);

    for (const s of services) {
      expect(typeof s.service_type).toBe("string");
      expect(typeof s.display_name).toBe("string");
      expect(typeof s.role).toBe("string");
      expect(Array.isArray(s.protocols)).toBe(true);
      expect(Array.isArray(s.point_fields)).toBe(true);
      expect(Array.isArray(s.plan_fields)).toBe(true);

      for (const p of s.protocols) {
        expect(typeof p.protocol).toBe("string");
        expect(typeof p.description).toBe("string");
        expect(Array.isArray(p.selection_rules)).toBe(true);
      }
    }
  });
});
