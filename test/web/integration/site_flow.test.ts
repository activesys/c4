// @vitest-environment node
// L2 场站初始化集成测试 — GET/POST /api/site（2026-10-05 用户指令：首次启动
// 须由用户提供场站信息）。复用真实 agent fixture（tmp config-dir，无 site 字段
// → 天然未绑定初始态）。

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { fetchState } from "@frontend/api/state";
import { bindSite, fetchSite } from "@frontend/api/site";
import { startAgent, type AgentHandle } from "./fixtures";

let agent: AgentHandle;

beforeAll(async () => {
  agent = await startAgent();
}, 120_000);

afterAll(async () => {
  await agent.stop();
}, 30_000);

describe("场站初始化（/api/site）", () => {
  it("初始未绑定：GET 返回 null，/api/state.siteName 为 null", async () => {
    expect(await fetchSite()).toBeNull();
    const state = await fetchState();
    expect(state.siteName).toBeNull();
  });

  it("绑定成功：POST 落盘 + GET 可读 + /api/state 实时推送", async () => {
    const site = await bindSite("华能阿拉善", "hnals");
    expect(site).toEqual({ name: "华能阿拉善", abbr: "hnals" });

    expect(await fetchSite()).toEqual({ name: "华能阿拉善", abbr: "hnals" });
    const state = await fetchState();
    expect(state.siteName).toBe("华能阿拉善");
  });

  it("重复绑定覆盖（幂等）：POST 新值后 GET 反映新值", async () => {
    await bindSite("华能阿拉善盟", "hnalsm");
    expect(await fetchSite()).toEqual({ name: "华能阿拉善盟", abbr: "hnalsm" });
    const state = await fetchState();
    expect(state.siteName).toBe("华能阿拉善盟");
  });

  it("非法名称：400 且不落盘", async () => {
    await expect(bindSite("长", "")).rejects.toThrow(/2~20/);
    await expect(bindSite("含,逗号", "")).rejects.toThrow(/2~20/);
    // 名称含逗号被拒 → 绑定保持上一次的值
    expect(await fetchSite()).toEqual({ name: "华能阿拉善盟", abbr: "hnalsm" });
  });

  it("非法缩写：400", async () => {
    await expect(bindSite("华能阿拉善", "缩写")).rejects.toThrow(/2~12/);
    await expect(bindSite("华能阿拉善", "a")).rejects.toThrow(/2~12/);
  });

  it("缩写缺省：自动生成（LLM 或名称派生），结果为 2~12 位字母数字", async () => {
    const site = await bindSite("WindFarm01", "");
    expect(site.name).toBe("WindFarm01");
    expect(site.abbr).toMatch(/^[a-z0-9]{2,12}$/);
  });
});
