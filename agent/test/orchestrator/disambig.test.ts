// c4/agent/test/orchestrator/disambig.test.ts — 消歧前缀指认单测（§3.2.1.3a）
// 「指认的确定性消费」：英文 token 与注册表前缀精确匹配、唯一命中即定目标；
// 含下划线的点 key 形态取首段作候选；多命中/无命中宁可放过（null）。
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import {
    parse_disambig_target,
    resolve_disambig_anchor,
} from "../../src/orchestrator/orchestrator.js";

function devices() {
    return [
        {
            id: "channel1",
            name: "1号风机",
            prefix: "wt1",
            service_type: "c4_asfp2_server",
            points: [],
            hostPoints: [],
        },
        {
            id: "channel2",
            name: "1号风机",
            prefix: "wt2",
            service_type: "c4_asfp2_server",
            points: [],
            hostPoints: [],
        },
    ] as Array<Record<string, unknown>>;
}

describe("parse_disambig_target（§3.2.1.3a 指认的确定性消费）", () => {
    it("唯一前缀 token 命中 → 定目标（消歧文案建议的指认形态）", () => {
        const hit = parse_disambig_target("给 wt1 那台加点", devices());
        expect(hit?.["prefix"]).toBe("wt1");
        expect(parse_disambig_target("wt1 删除", devices())?.["id"]).toBe("channel1");
    });

    it("多命中（消息同时含两个候选前缀）→ null（宁可放过不可错认）", () => {
        expect(parse_disambig_target("wt1 和 wt2 都删除", devices())).toBeNull();
    });

    it("无命中 → null（交正常解析路径）", () => {
        expect(parse_disambig_target("给 3 号风机加点", devices())).toBeNull();
        expect(parse_disambig_target("取消", devices())).toBeNull();
    });

    it("完整点 key（wt1_temperature）取首段作前缀候选，仍可指认", () => {
        const hit = parse_disambig_target("删除 wt1_temperature 点", devices());
        expect(hit?.["prefix"]).toBe("wt1");
    });

    it("他设备前缀的点 key 不误指认（wt1_temperature vs 候选仅 wt2）", () => {
        const onlyWt2 = devices().slice(1);
        expect(parse_disambig_target("wt1_temperature 的地址是多少", onlyWt2)).toBeNull();
    });
});

describe("resolve_disambig_anchor（§3.2.1.3a 锚状态机：新鲜指认 > 陈旧锚）", () => {
    it("双状态并存（陈旧锚 + 消歧再询问）时，新鲜指认优先——改选 wt2 必须生效", () => {
        const r = resolve_disambig_anchor(true, "channel1", "wt2 删除", devices());
        expect(r.anchor?.["prefix"]).toBe("wt2");
        expect(r.hostId).toBe("channel2");
        expect(r.pending).toBe(false);
    });

    it("新鲜指认失败（消息无候选 token）→ 回落陈旧锚（已选定延续语义）", () => {
        const r = resolve_disambig_anchor(true, "channel1", "删除它", devices());
        expect(r.anchor?.["prefix"]).toBe("wt1");
        expect(r.hostId).toBe("channel1");
    });

    it("无语境延续陈旧锚；锚设备不存在 → 锚失效全清", () => {
        const keep = resolve_disambig_anchor(false, "channel1", "加点", devices());
        expect(keep.anchor?.["prefix"]).toBe("wt1");
        const gone = resolve_disambig_anchor(false, "channel9", "加点", devices());
        expect(gone.anchor).toBeNull();
        expect(gone.hostId).toBeNull();
    });

    it("无语境无锚 → 空结果（交正常解析路径）", () => {
        const r = resolve_disambig_anchor(false, null, "删除1号风机", devices());
        expect(r.anchor).toBeNull();
        expect(r.hostId).toBeNull();
    });
});
