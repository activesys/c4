// c4/agent/test/registry/device_prefix.test.ts — 设备前缀派生单测（agent.md §3.2.1.3c）
// 2026-10-19 命名规范切换：前缀 = 设备名原样归一化（1#风机_风速），类型缩写+编号
// 体系（wt1/zy1/nbA01/syz）退役；匿名 → dev 基名；同名不顺延（调用方拒收）。
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import {
    device_prefix_candidate,
    device_type_known,
    group_member_prefix,
} from "../../src/registry/device_prefix.js";

describe("device_prefix_candidate（设备名原样归一化）", () => {
    it("编号设备名原样保留（用户给定示例形态）", () => {
        expect(device_prefix_candidate("1号风机")).toBe("1号风机");
        expect(device_prefix_candidate("1#风机")).toBe("1#风机");
        expect(device_prefix_candidate("2号变压器")).toBe("2号变压器");
        expect(device_prefix_candidate("1号风机变桨控制器")).toBe("1号风机变桨控制器");
    });

    it("裸类型词设备：升压站 → 升压站（单台无编号合法名，§3.2.1.3c 例外）", () => {
        expect(device_prefix_candidate("升压站")).toBe("升压站");
        expect(device_prefix_candidate("2号升压站")).toBe("2号升压站");
    });

    it("中文数字/区段名不翻译，原样进前缀", () => {
        expect(device_prefix_candidate("三号风机")).toBe("三号风机");
        expect(device_prefix_candidate("二十三区光伏")).toBe("二十三区光伏");
        expect(device_prefix_candidate("14区1#逆变器")).toBe("14区1#逆变器");
    });

    it("ASCII 设备名保留下划线（前缀可含 `_`，归属改由注册表前缀边界匹配）", () => {
        expect(device_prefix_candidate("power_forecast_1")).toBe("power_forecast_1");
    });

    it("key 非法字符由归一化统一处理（`.`/空白 → `_`；ASCII 小写）", () => {
        expect(device_prefix_candidate("1.号风机")).toBe("1_号风机");
        expect(device_prefix_candidate("WT A")).toBe("wt_a");
    });

    it("纯符号/空名归一化为空 → dev 基名（注册表分配 dev{N} 序列）", () => {
        expect(device_prefix_candidate("...")).toBe("dev");
        expect(device_prefix_candidate("")).toBe("dev");
    });
});

describe("device_type_known / group_member_prefix（组模式 §2.11）", () => {
    it("13 类封闭词表 + 历史类型词可识别", () => {
        for (const t of ["风机", "升压站", "电计量", "计量小站", "风功率", "测风塔", "光伏",
            "箱变", "逆变器", "汇流箱", "故障录波", "保信", "气象数据", "主变", "变压器"]) {
            expect(device_type_known(t)).toBe(true);
        }
        expect(device_type_known("锅炉")).toBe(false);
    });

    it("成员前缀 = 成员设备名原样归一化（1号风机、A01逆变器）", () => {
        expect(group_member_prefix("风机", "1号风机")).toBe("1号风机");
        expect(group_member_prefix("逆变器", "A01逆变器")).toBe("a01逆变器");
        expect(group_member_prefix("箱变", "箱变123")).toBe("箱变123");
    });

    it("类型词不可识别 → null（组级缺口，不猜）", () => {
        expect(group_member_prefix("锅炉", "1号锅炉")).toBeNull();
    });
});
