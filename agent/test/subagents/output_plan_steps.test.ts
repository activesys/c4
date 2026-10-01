// c4/agent/test/subagents/output_plan_steps.test.ts — step-decomposer 身份规则单测
// agent.md §3.2.1.3（2026-10-01 设计修订）：
//   - 实例 id = channel{N} 顺序句柄（channelStart 起递增，writer/reader 相邻）
//   - 点 key = {设备前缀}_{裸id} 无条件前缀；转发点 key = {writer实例id}.{点key}
//   - 显式 instanceId/action（同端口并入 action=modify）原样消费
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import { generate_steps } from "../../src/subagents/tools/output_plan_steps.js";
import type { McpServiceRegistry } from "../../src/registry/registry.js";
import type { RegistryEntry } from "../../src/types/index.js";

function fake_registry(): McpServiceRegistry {
    const entries: Record<string, RegistryEntry> = {
        c4_asfp2_server: {
            role: "writer",
            protocols: [{ protocol: "asfp2" }],
            point_schema: {
                fields: [
                    { name: "addr", type: "integer" },
                    { name: "name", type: "string" },
                ],
                identity_fields: ["addr"],
            },
            config_schema: {
                fields: {
                    port: { type: "integer", description: "监听端口" },
                    t1: { type: "integer", default: 0 },
                },
            },
        },
        c4_asfp2_client: {
            role: "reader",
            protocols: [{ protocol: "asfp2" }],
            point_schema: {
                fields: [{ name: "addr", type: "integer" }],
                identity_fields: ["addr"],
            },
            config_schema: {
                fields: {
                    ip: { type: "string" },
                    port: { type: "integer" },
                    t0: { type: "integer", default: 30 },
                },
            },
        },
    } as unknown as Record<string, RegistryEntry>;
    return {
        getServiceCatalogEntries: () => [
            {
                role: "writer",
                service_type: "c4_asfp2_server",
                protocols: [{ protocol: "asfp2" }],
            },
            {
                role: "reader",
                service_type: "c4_asfp2_client",
                protocols: [{ protocol: "asfp2" }],
            },
        ],
        get_entry: (st: string) => entries[st],
        queryRegistry: (st: string) => entries[st],
        getServiceTypes: () => Object.keys(entries),
    } as unknown as McpServiceRegistry;
}

function plan_input() {
    return {
        devices: [
            {
                name: "1号风机",
                prefix: "wt1",
                protocol: "asfp2",
                port: 9001,
                points: [
                    { name: "风速", id: "windspeed", addr: 1000 },
                    { name: "温度", id: "temperature", addr: 1001 },
                ],
            },
        ],
        forward_targets: [
            {
                name: "转发到中心",
                protocol: "asfp2",
                ip: "172.16.109.11",
                port: 9999,
                points: [{ addr: 3001 }, { addr: 3002 }],
            },
        ],
    };
}

describe("generate_steps 身份规则（§3.2.1.3）", () => {
    it("无显式身份 → channel 序号自动分配，writer/reader 相邻", () => {
        const r = generate_steps(plan_input(), fake_registry(), { channelStart: 0 });
        expect(r.fatal).toBeNull();
        expect(r.steps[0].instance["id"]).toBe("channel1");
        expect(r.steps[1].instance["id"]).toBe("channel2");
    });

    it("channelStart 起始分配", () => {
        const r = generate_steps(plan_input(), fake_registry(), { channelStart: 4 });
        expect(r.steps[0].instance["id"]).toBe("channel5");
        expect(r.steps[1].instance["id"]).toBe("channel6");
    });

    it("点 key = {prefix}_{裸id}；转发点 key = {writer实例id}.{点key}", () => {
        const r = generate_steps(plan_input(), fake_registry(), { channelStart: 0 });
        const wpts = r.steps[0].points as Array<Record<string, unknown>>;
        expect(wpts[0]["id"]).toBe("wt1_windspeed");
        expect(wpts[1]["id"]).toBe("wt1_temperature");
        const rpts = r.steps[1].points as Array<Record<string, unknown>>;
        expect(rpts[0]["key"]).toBe("channel1.wt1_windspeed");
        expect(rpts[1]["key"]).toBe("channel1.wt1_temperature");
    });

    it("显式 instanceId/action（同端口并入）原样消费", () => {
        const input = plan_input();
        (input.devices[0] as Record<string, unknown>)["instanceId"] = "channel1";
        (input.devices[0] as Record<string, unknown>)["action"] = "modify";
        const r = generate_steps(input, fake_registry(), { channelStart: 1 });
        expect(r.fatal).toBeNull();
        expect(r.steps[0].action).toBe("modify");
        expect(r.steps[0].instance["id"]).toBe("channel1");
    });

    it("同一设备内部真重名（同前缀同裸 id）→ fatal 拒绝", () => {
        const input = plan_input();
        input.devices[0].points.push({ name: "风速2", addr: 1100 });
        input.devices[0].points[2] = {
            ...input.devices[0].points[2],
            id: "windspeed", // 与第 1 点同裸 id → wt1_windspeed 撞 key
        };
        const r = generate_steps(input, fake_registry(), { channelStart: 0 });
        expect(r.fatal).toContain("wt1_windspeed");
    });

    it("组合回归（P0）：装配层已带前缀的 id / pointMap 沿用 key → 不产生双重前缀", () => {
        // 模拟 assemble_access_plan 的实际产出形态：点 id 已是完整 key
        const input = plan_input();
        input.devices[0].points[0]["id"] = "wt1_windspeed";
        // 模拟 pointMap 沿用的旧 key 形态（同前缀，剥后重拼幂等）
        input.devices[0].points[1]["id"] = "wt1_temperature";
        const r = generate_steps(input, fake_registry(), { channelStart: 0 });
        expect(r.fatal).toBeNull();
        const wpts = r.steps[0].points as Array<Record<string, unknown>>;
        expect(wpts[0]["id"]).toBe("wt1_windspeed");
        expect(wpts[1]["id"]).toBe("wt1_temperature");
        const rpts = r.steps[1].points as Array<Record<string, unknown>>;
        expect(rpts[0]["key"]).toBe("channel1.wt1_windspeed");
    });

    it("跨前缀边界（P0 复验）：他设备前缀的 id 不被误剥（syz + wt1_windspeed → syz_wt1_windspeed）", () => {
        const input = plan_input();
        (input.devices[0] as Record<string, unknown>)["prefix"] = "syz";
        input.devices[0].points[0]["id"] = "wt1_windspeed"; // 恰似点 key 的裸 id
        const r = generate_steps(input, fake_registry(), { channelStart: 0 });
        expect(r.fatal).toBeNull();
        const wpts = r.steps[0].points as Array<Record<string, unknown>>;
        expect(wpts[0]["id"]).toBe("syz_wt1_windspeed");
    });

    it("混合重复边界（P0 复验）：裸 id 与已前缀 id 同批（windspeed + wt1_windspeed）→ 撞 key 拦截", () => {
        const input = plan_input();
        input.devices[0].points.push({ name: "风速3", id: "wt1_windspeed", addr: 1100 });
        const r = generate_steps(input, fake_registry(), { channelStart: 0 });
        expect(r.fatal).toContain("wt1_windspeed");
    });
});
