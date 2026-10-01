// c4/agent/test/registry/device_prefix.test.ts — 设备前缀派生单测（agent.md §3.2.1.3c）
// 类型映射表（含 2026-10-01 补的升压站→syz）+ 编号（阿拉伯/中文数字）+ 匿名 dev 兜底。
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import { device_prefix_candidate } from "../../src/registry/device_prefix.js";

describe("device_prefix_candidate", () => {
    it("类型映射：风机→wt、主变→zy、逆变器→nb、测风塔→cft、光伏→gf、储能→cn", () => {
        expect(device_prefix_candidate("1号风机")).toBe("wt1");
        expect(device_prefix_candidate("2#风机")).toBe("wt2");
        expect(device_prefix_candidate("1号主变")).toBe("zy1");
        expect(device_prefix_candidate("逆变器3")).toBe("nb3");
        expect(device_prefix_candidate("测风塔")).toBe("cft");
        expect(device_prefix_candidate("光伏区1")).toBe("gf1");
        expect(device_prefix_candidate("储能舱")).toBe("cn");
    });

    it("升压站→syz（2026-10-01 补，原表缺失此条目曾落 dev 兜底）", () => {
        expect(device_prefix_candidate("升压站")).toBe("syz");
        expect(device_prefix_candidate("2号升压站")).toBe("syz2");
    });

    it("中文数字编号：三号风机 → wt3", () => {
        expect(device_prefix_candidate("三号风机")).toBe("wt3");
        expect(device_prefix_candidate("十号测风塔")).toBe("cft10");
    });

    it("单台无编号 → 纯类型缩写（点 key 如 syz_active_power）", () => {
        expect(device_prefix_candidate("升压站")).toBe("syz");
    });

    it("表未命中 → ASCII（不含下划线，§3.2.1.3c）；完全匿名 → dev 基名", () => {
        expect(device_prefix_candidate("power_forecast")).toBe("powerforecast");
        expect(device_prefix_candidate("没有名字")).toBe("dev");
    });
});
