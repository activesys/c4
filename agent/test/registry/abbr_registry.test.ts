// c4/agent/test/registry/abbr_registry.test.ts — 设备身份注册表单测
// agent.md §3.2.1.3/§3.2.1.3a（2026-10-01 设计修订）：
//   - 条目 = 设备名 → {宿主实例, 点前缀} + pointMap；channelHighWatermark 永不回退
//   - 同名命中 → 描述匹配仲裁；多条无法区分 → name_conflict
//   - 前缀撞名顺延 / 匿名 dev{N} 序列 / channel 未用最小序号
//   - 旧格式（id/abbr 条目）不兼容，直接废弃重建
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import {
    bump_channel_watermark,
    channel_watermark_from_config,
    delete_entry,
    finalize_entry,
    next_channel_id,
    next_dev_prefix,
    rebuild_entries,
    resolve_prefix_conflict,
    retrieve_device,
    type AbbrRegistry,
} from "../../src/registry/abbr_registry.js";
import type { SystemConfig } from "../../src/types/index.js";

function empty_registry(): AbbrRegistry {
    return { channelHighWatermark: 0, entries: [] };
}

function config_with_points(): SystemConfig {
    return {
        c4_shm_manager: { writer: ["c4_asfp2_server"], reader: [] },
        c4_asfp2_server: [
            {
                id: "channel1",
                name: "数据接收服务",
                port: 9001,
                points: [
                    { id: "wt1_windspeed", name: "风速", addr: 1000, shm_id: 1 },
                    { id: "wt1_temperature", name: "温度", addr: 1001, shm_id: 2 },
                    { id: "wt2_windspeed", name: "风速", addr: 1100, shm_id: 3 },
                ],
            },
        ],
    } as unknown as SystemConfig;
}

describe("retrieve_device（§3.2.1.3a 描述匹配仲裁）", () => {
    const reg: AbbrRegistry = {
        channelHighWatermark: 1,
        entries: [
            {
                name: "1号风机",
                prefix: "wt1",
                host: "channel1",
                service_type: "c4_asfp2_server",
                description: "采集1号风机的数据",
                pointMap: { 风速: "wt1_windspeed" },
            },
        ],
    };

    it("同名单条命中（描述弱信号/复述名字）→ same_device，复用 host/prefix", () => {
        const r = retrieve_device(reg, "1#风机"); // 分隔符归一：1#风机 ≡ 1号风机
        expect(r.decision).toBe("same_device");
        expect(r.entry?.prefix).toBe("wt1");
        expect(r.entry?.host).toBe("channel1");
    });

    it("单命中但描述完全对不上 → name_conflict（§3.2.1.3a 仲裁对单命中同样生效）", () => {
        const r = retrieve_device(reg, "1号风机", "二号升压站汇控柜数据，与该风机无关");
        expect(r.decision).toBe("name_conflict");
        expect(r.candidates?.length).toBe(1);
    });

    it("无同名 → no_hit（新设备）", () => {
        expect(retrieve_device(reg, "2号风机").decision).toBe("no_hit");
    });

    it("同名多条且描述无法区分 → name_conflict（追问用户）", () => {
        const multi: AbbrRegistry = {
            channelHighWatermark: 2,
            entries: [
                { ...reg.entries[0] },
                { ...reg.entries[0], prefix: "wt2", description: "另一台1号风机" },
            ],
        };
        const r = retrieve_device(multi, "1号风机", "完全不同的描述");
        expect(r.decision).toBe("name_conflict");
        expect(r.candidates?.length).toBe(2);
    });
});

describe("前缀与序号分配（§3.2.1.3/§3.2.1.3c）", () => {
    it("撞名顺延：wt1 已占用 → wt2（保留前缀 + 最小未用编号）", () => {
        const reg: AbbrRegistry = {
            channelHighWatermark: 1,
            entries: [
                {
                    name: "1号风机",
                    prefix: "wt1",
                    host: "channel1",
                    service_type: "x",
                    description: "",
                    pointMap: {},
                },
            ],
        };
        expect(resolve_prefix_conflict(reg, "wt1")).toBe("wt2");
        expect(resolve_prefix_conflict(reg, "wt9")).toBe("wt9"); // 未占用不改动
        expect(resolve_prefix_conflict(reg, "syz")).toBe("syz");
        reg.entries.push({
            name: "9号风机", prefix: "wt9", host: "channel9",
            service_type: "x", description: "", pointMap: {},
        });
        expect(resolve_prefix_conflict(reg, "wt9")).toBe("wt10"); // 占用顺延
    });

    it("匿名序列 dev{N}：取最小未用编号", () => {
        const reg = empty_registry();
        expect(next_dev_prefix(reg)).toBe("dev1");
        reg.entries.push({
            name: "dev1", prefix: "dev1", host: "channel1",
            service_type: "x", description: "", pointMap: {},
        });
        expect(next_dev_prefix(reg)).toBe("dev2");
    });

    it("channel 未用最小序号 = 水印 + 1；bump 只增不减", () => {
        const reg = empty_registry();
        expect(next_channel_id(reg)).toBe("channel1");
        bump_channel_watermark(reg, "channel3");
        expect(reg.channelHighWatermark).toBe(3);
        expect(next_channel_id(reg)).toBe("channel4");
        bump_channel_watermark(reg, "channel2");
        expect(reg.channelHighWatermark).toBe(3);
    });

    it("channelHighWatermark 从现存实例重建（不可重建的已删序号按可接受降级）", () => {
        expect(channel_watermark_from_config(config_with_points())).toBe(1);
    });
});

describe("finalize / delete / rebuild", () => {
    it("finalize 按 prefix 主键 upsert（返回新 registry）", () => {
        let reg = empty_registry();
        const e = {
            name: "1号风机", prefix: "wt1", host: "channel1",
            service_type: "c4_asfp2_server", description: "", pointMap: {},
        };
        reg = finalize_entry(reg, e);
        reg = finalize_entry(reg, { ...e, pointMap: { 风速: "wt1_windspeed" } });
        expect(reg.entries.length).toBe(1);
        expect(reg.entries[0].pointMap["风速"]).toBe("wt1_windspeed");
    });

    it("delete 按 prefix 物理删除，水印不回退", () => {
        const reg: AbbrRegistry = {
            channelHighWatermark: 4,
            entries: [
                {
                    name: "2号风机", prefix: "wt2", host: "channel4",
                    service_type: "x", description: "", pointMap: {},
                },
            ],
        };
        const next = delete_entry(reg, "wt2");
        expect(next.entries.length).toBe(0);
        expect(next.channelHighWatermark).toBe(4);
    });

    it("rebuild_entries 从 config 按点 key 前缀分组重建 + pointMap 同源重建", () => {
        const entries = rebuild_entries(config_with_points());
        const wt1 = entries.find((e) => e.prefix === "wt1");
        const wt2 = entries.find((e) => e.prefix === "wt2");
        expect(wt1?.host).toBe("channel1");
        expect(wt1?.pointMap["风速"]).toBe("wt1_windspeed");
        expect(wt1?.pointMap["温度"]).toBe("wt1_temperature");
        expect(wt2?.pointMap["风速"]).toBe("wt2_windspeed");
    });
});
