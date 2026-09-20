// c4/agent/test/executor/point_rules.test.ts
// point_rules 共享契约单元测试（agent.md §2.4.3；vitest）
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import {
    BIT_FUNS,
    REGISTER_SPAN,
    check_duplicate_points,
    check_fun_codes,
    check_required_fields,
    check_shm_overlap,
    check_uid_provenance,
    point_identity,
    register_span,
    span_error,
    validate_point_table,
    type PointLike,
} from "../../src/executor/point_rules.js";

const pt = (addr: number, type = 10, uid = 2, fun = 3, name?: string): PointLike => ({
    addr, type, uid, fun, ...(name ? { name } : {}),
});

describe("register_span——17 型跨度全表（对齐 protocol/const.go）", () => {
    it("1 寄存器类型：Boolean/Bit/Int8/Uint8/Int16/Uint16/Float16", () => {
        for (const t of [0, 15, 1, 2, 3, 4, 9]) {
            expect(REGISTER_SPAN[t]).toBe(1);
            expect(register_span(t)).toBe(1);
        }
    });

    it("2 寄存器类型：Int32/Uint32/Float32", () => {
        for (const t of [5, 6, 10]) {
            expect(REGISTER_SPAN[t]).toBe(2);
            expect(register_span(t)).toBe(2);
        }
    });

    it("4 寄存器类型：Int64/Uint64/Float64（今日事故类的完整覆盖面）", () => {
        for (const t of [7, 8, 11]) {
            expect(REGISTER_SPAN[t]).toBe(4);
            expect(register_span(t)).toBe(4);
        }
    });

    it("变长类型 fail-closed：String/Blob/Bitstring/LargeDataBlock → null", () => {
        for (const t of [12, 13, 14, 16]) {
            expect(register_span(t)).toBeNull();
        }
    });

    it("未知/非法类型 fail-closed → null", () => {
        for (const t of [17, 99, 255, -1, Number.NaN, undefined]) {
            expect(register_span(t as number | undefined)).toBeNull();
        }
    });

    it("span_error 输出可读错误（含类型值与改型建议）", () => {
        const msg = span_error(12, "第 3 个点");
        expect(msg).toContain("第 3 个点");
        expect(msg).toContain("变长或未知类型");
    });
});

describe("point_identity——身份键规范化", () => {
    it("身份齐全 → uid/fun/addr 键", () => {
        expect(point_identity(pt(3008))).toBe("uid=2, fun=3, addr=3008");
    });

    it("身份不全 → null（层级适用范围：跳过判定）", () => {
        expect(point_identity({ addr: 3008 })).toBeNull();
        expect(point_identity({ uid: 2, addr: 3008 })).toBeNull();
        expect(point_identity({ uid: 2, fun: 3 })).toBeNull();
    });
});

describe("check_duplicate_points——身份组合重复（uid+fun+addr）", () => {
    it("今日事故变体：同组同地址两个点 → 报错并列出双方", () => {
        const errors = check_duplicate_points([
            pt(3008, 10, 2, 3, "后备电源电压"),
            pt(3008, 10, 2, 3, "后备电源温度"),
        ]);
        expect(errors).toHaveLength(1);
        expect(errors[0]).toContain("uid=2, fun=3, addr=3008");
        expect(errors[0]).toContain("后备电源电压");
        expect(errors[0]).toContain("后备电源温度");
        expect(errors[0]).toContain("未写入任何变更");
    });

    it("地址唯一 → 通过", () => {
        expect(check_duplicate_points([pt(3008), pt(3009), pt(3010)])).toHaveLength(0);
    });

    it("不同 uid / 不同 fun 的同地址 → 不算重复", () => {
        expect(check_duplicate_points([pt(3008, 10, 2, 3), pt(3008, 10, 3, 3), pt(3008, 10, 2, 4)]))
            .toHaveLength(0);
    });

    it("身份不全的点跳过（access_plan 层适用范围，m7）", () => {
        expect(check_duplicate_points([{ addr: 3008 }, pt(3008)])).toHaveLength(0);
    });
});

describe("check_shm_overlap——同 (uid,fun) 组内 [addr, addr+span) 区间", () => {
    it("今日事故本体：float32 3008+3009 相邻重叠", () => {
        const errors = check_shm_overlap([pt(3008, 10, 2, 3, "后备电源电压"),
            pt(3009, 10, 2, 3, "后备电源温度")]);
        expect(errors).toHaveLength(1);
        expect(errors[0]).toContain("addr=3009");
        expect(errors[0]).toContain("addr=3008");
        expect(errors[0]).toContain("跨度 2");
        expect(errors[0]).toContain("(uid=2, fun=3)");
        expect(errors[0]).toContain("未写入任何变更");
    });

    it("裁决正确形态：3008+3010 相邻但区间 [3008,3010) 与 [3010,3012) 相接不相交 → 通过", () => {
        expect(check_shm_overlap([pt(3008), pt(3010)])).toHaveLength(0);
    });

    it("int64（跨度 4）覆盖判定：3006+[3006,3010) vs 3009 → 重叠；vs 3010 → 通过", () => {
        expect(check_shm_overlap([pt(3006, 11), pt(3009, 10)])).toHaveLength(1);
        expect(check_shm_overlap([pt(3006, 11), pt(3010, 10)])).toHaveLength(0);
    });

    it("乱序输入不影响判定", () => {
        expect(check_shm_overlap([pt(3009), pt(3008)])).toHaveLength(1);
    });

    it("不同 uid / 不同 fun 同地址 → 不重叠", () => {
        expect(check_shm_overlap([pt(3008, 10, 2, 3), pt(3008, 10, 3, 3)])).toHaveLength(0);
        expect(check_shm_overlap([pt(3008, 10, 2, 3), pt(3008, 10, 2, 4)])).toHaveLength(0);
    });

    it("fun∈{1,2} 位编址：相邻/同址不触发跨度重叠（由身份查重覆盖）", () => {
        expect(BIT_FUNS.has(1) && BIT_FUNS.has(2)).toBe(true);
        expect(check_shm_overlap([pt(0, 0, 2, 1), pt(1, 0, 2, 1)])).toHaveLength(0);
    });

    it("变长类型 fail-closed：string 点返回可读错误而非静默跳过", () => {
        const errors = check_shm_overlap([pt(3000, 12)]);
        expect(errors).toHaveLength(1);
        expect(errors[0]).toContain("变长或未知类型");
    });

    it("身份不全的点跳过", () => {
        expect(check_shm_overlap([{ addr: 3009 }, pt(3008)])).toHaveLength(0);
    });
});

describe("check_required_fields——逐点必填字段", () => {
    const REQUIRED = ["uid", "fun", "type", "swap"] as const;

    it("缺失字段逐点列出，多缺失分号连接", () => {
        const errors = check_required_fields(
            [{ addr: 3000, name: "桨叶角度" }, { addr: 3002, fun: 3 }] as PointLike[],
            REQUIRED,
            "1号风机变桨控制器",
        );
        expect(errors).toHaveLength(2);
        expect(errors[0]).toContain("第 1 个点");
        expect(errors[0]).toContain('缺少必要字段 "uid"');
        expect(errors[0]).toContain('缺少必要字段 "fun"');
        expect(errors[0]).toContain('缺少必要字段 "type"');
        expect(errors[1]).toContain('缺少必要字段 "uid"');
        expect(errors[1]).not.toContain('"fun"');
    });

    it("全齐 → 通过", () => {
        expect(check_required_fields([pt(3000)], ["uid", "fun", "type"])).toHaveLength(0);
    });

    it("空串/null 视同缺失", () => {
        const errors = check_required_fields(
            [{ addr: 3000, uid: "", fun: null as unknown as number }] as PointLike[],
            ["uid", "fun"],
        );
        expect(errors).toHaveLength(1);
        expect(errors[0]).toContain('"uid"');
        expect(errors[0]).toContain('"fun"');
    });
});

describe("check_fun_codes——Modbus 功能码合法性（2026-09-19 fun=9 事故）", () => {
    it("fun=9 非法 → 逐点可读错误，列出合法功能码", () => {
        const errors = check_fun_codes([
            pt(3000, 10, 2, 9, "桨叶角度"),
            pt(3002, 10, 2, 9, "变桨速度"),
        ]);
        expect(errors).toHaveLength(2);
        expect(errors[0]).toContain("功能码 9 非法");
        expect(errors[0]).toContain("3(保持寄存器)");
    });

    it("合法功能码 1/2/3/4 → 通过", () => {
        expect(check_fun_codes([pt(0, 0, 2, 1), pt(1, 0, 2, 2), pt(3000, 10, 2, 3), pt(3002, 10, 2, 4)]))
            .toHaveLength(0);
    });

    it("fun 缺失（非 modbus 点）跳过", () => {
        expect(check_fun_codes([{ addr: 1000, name: "风速" }])).toHaveLength(0);
    });
});

describe("check_uid_provenance——从站号来源校验（2026-09-19 uid=1 编造事故）", () => {
    it("用户未提供从站号且点带 uid → 编造错误", () => {
        const errors = check_uid_provenance([pt(3000, 10, 1, 3), pt(3002, 10, 1, 3)], new Set());
        expect(errors).toHaveLength(1);
        expect(errors[0]).toContain("未由用户提供");
        expect(errors[0]).toContain("禁止编造");
    });

    it("用户声明从站号 1 → uid=1 通过；声明 2 → uid=1 不符", () => {
        expect(check_uid_provenance([pt(3000, 10, 1, 3)], new Set([1]))).toHaveLength(0);
        const errors = check_uid_provenance([pt(3000, 10, 1, 3)], new Set([2]));
        expect(errors).toHaveLength(1);
        expect(errors[0]).toContain("uid=1 与用户提供的从站号（2）不符");
    });

    it("多从站声明均接受", () => {
        expect(check_uid_provenance(
            [pt(3000, 10, 1, 3), pt(4001, 10, 2, 3)],
            new Set([1, 2]),
        )).toHaveLength(0);
    });

    it("uid 缺失跳过（必填字段校验负责）", () => {
        expect(check_uid_provenance([{ addr: 3000 }], new Set())).toHaveLength(0);
    });
});

describe("validate_point_table——一站式（merge 前置）", () => {
    it("三类违例同时报告：必填缺失 + 身份重复 + 区间重叠", () => {
        const errors = validate_point_table(
            [pt(3008, 10, 2, 3, "电压"), pt(3008, 10, 2, 3, "温度副本"), pt(3009, 10, 2, 3, "温度")],
            { required: ["uid", "fun", "type", "swap"], label: "1号风机变桨控制器" },
        );
        expect(errors.some((e) => e.includes('缺少必要字段 "swap"'))).toBe(true);
        expect(errors.some((e) => e.includes("点重复"))).toBe(true);
        expect(errors.some((e) => e.includes("点重叠"))).toBe(true);
    });

    it("merge 后最终点表（批次 + 既有点合并传入）→ 批次与既有点的重叠被捕获", () => {
        const existing = pt(3008, 10, 2, 3, "后备电源电压");
        const batch = pt(3009, 10, 2, 3, "后备电源温度");
        expect(validate_point_table([existing, batch])).toHaveLength(1);
    });
});
