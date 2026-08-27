// @vitest-environment node
// L2 状态轮询集成测试 — web.md §3.4（用例 4.4.1 / 4.4.2）

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { fetchState } from "@frontend/api/state";
import { startAgent, type AgentHandle } from "./fixtures";

const VALID_PHASES = ["idle", "collecting", "planning", "confirmed", "executing"];

let agent: AgentHandle;

beforeAll(async () => {
  agent = await startAgent();
}, 120_000);

afterAll(async () => {
  await agent.stop();
}, 30_000);

describe("状态轮询（真实后端）", () => {
  it("4.4.1 fetchState 返回 phase 属于 5 值集合", async () => {
    const state = await fetchState();

    expect(VALID_PHASES).toContain(state.phase);
    expect(typeof state.hasAccessPlan).toBe("boolean");
    expect(state.lastError === null || typeof state.lastError === "string").toBe(
      true,
    );
  });

  it("4.4.2 phase 容忍跳变：多次轮询均落在合法值集合内", async () => {
    const seen = new Set<string>();
    for (let i = 0; i < 3; i++) {
      const state = await fetchState();
      seen.add(state.phase);
      expect(VALID_PHASES).toContain(state.phase);
      await new Promise((resolve) => setTimeout(resolve, 300));
    }

    // 徽标只反映最近读到的 phase；所有采样值合法即通过（web.md §3.4.2）
    expect(seen.size).toBeGreaterThanOrEqual(1);
  });
});
