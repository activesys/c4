// c4/agent/test/orchestrator/influx_field.test.ts — influx field 显式性门禁单测
// 「字段名跟点名对应」无显式清单 = field 未显式给出（2026-10-19 influx 套件步39
// 实测：点名中文化后 LLM 把该词形当翻译授权，逐轮译名漂移——gearbox_oil_temp /
// gear_oil_temp 两种裁决都出现过，断言钉不住）→ 门禁剥离 LLM 提取值，交
// 「缺 field」缺口追问，显式清单确定性绑定闭合（2026-10-09 裁定的代码化）。
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import { field_automap_license_without_list } from "../../src/orchestrator/orchestrator.js";

describe("field_automap_license_without_list（自动映射许可词形且无清单）", () => {
    it("回归步39 原话（无清单）→ 未显式给出", () => {
        expect(field_automap_license_without_list(
            "10个点全部写进wind_turbine，字段名跟点名对应，类型统一float。",
        )).toBe(true);
    });

    it("显式清单形态 → 已显式给出（不剥除）", () => {
        expect(field_automap_license_without_list(
            "10个点全部写进wind_turbine这个measurement，字段名跟点名对应" +
            "（windspeed、power、wind_dir、pitch_angle、gen_speed、gearbox_oil_temp、" +
            "tower_temp、air_temp、humidity、pressure），类型统一float。",
        )).toBe(false);
    });

    it("无 field 语述 / 无关协议语述 → 不命中", () => {
        expect(field_automap_license_without_list(
            "10个点全部写进wind_turbine，类型统一float。",
        )).toBe(false);
        expect(field_automap_license_without_list(
            "转发采用asfp2协议，点表5000~5009。",
        )).toBe(false);
    });

    it("词形变体：字段跟点名对应 / 一一对应", () => {
        expect(field_automap_license_without_list("字段跟点名对应")).toBe(true);
        expect(field_automap_license_without_list("字段名跟点名一一对应")).toBe(true);
    });
});
