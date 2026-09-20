// c4/agent/test/super_worker/turn_rules.test.ts
// 回合状态机强化纯函数单元测试（agent.md §2.4.1 / §2.4.2）

import { describe, expect, it } from "vitest";
import {
    can_transition,
    check_tool_gate,
    question_hit,
    type GateSnapshot,
} from "../../src/super_worker/turn_rules.js";

const snap = (over: Partial<GateSnapshot> = {}): GateSnapshot => ({
    phase: "idle",
    questionPending: false,
    deviceInfoReady: false,
    userConfirmed: false,
    ...over,
});

describe("can_transition——合法转移表（§2.4.1，两轮评审后的表格）", () => {
    it("idle 只能进入 collecting", () => {
        expect(can_transition("idle", "collecting")).toBe(true);
        for (const to of ["planning", "confirmed", "executing"] as const) {
            expect(can_transition("idle", to)).toBe(false);
        }
    });

    it("collecting：补充收集/回 idle/出方案合法；确认与执行非法", () => {
        for (const to of ["idle", "collecting", "planning"] as const) {
            expect(can_transition("collecting", to)).toBe(true);
        }
        expect(can_transition("collecting", "confirmed")).toBe(false);
        expect(can_transition("collecting", "executing")).toBe(false);
    });

    it("planning：四向合法（含打回重收集与回合终结回 idle）", () => {
        for (const to of ["idle", "collecting", "planning", "confirmed"] as const) {
            expect(can_transition("planning", to)).toBe(true);
        }
        expect(can_transition("planning", "executing")).toBe(false);
    });

    it("confirmed：可重收集/重新出方案/开始执行；自转移非法（同方案不得二次确认）", () => {
        for (const to of ["idle", "collecting", "planning", "executing"] as const) {
            expect(can_transition("confirmed", to)).toBe(true);
        }
        expect(can_transition("confirmed", "confirmed")).toBe(false);
    });

    it("executing：完成/失败终结、失败重出方案、失败补问、幂等置位", () => {
        for (const to of ["idle", "collecting", "planning", "executing"] as const) {
            expect(can_transition("executing", to)).toBe(true);
        }
        expect(can_transition("executing", "confirmed")).toBe(false);
    });

    it("B2 回归：planning→collecting 与 confirmed→collecting 必须合法（device_info 前置放宽的依据）", () => {
        expect(can_transition("planning", "collecting")).toBe(true);
        expect(can_transition("confirmed", "collecting")).toBe(true);
    });
});

describe("question_hit——问询句式（§2.4.2，含排除清单）", () => {
    it("句末 ？命中", () => {
        expect(question_hit("请问端口号是多少？")).toBe(true);
    });

    it("句中问句（按句切分）命中", () => {
        expect(question_hit("请问端口是多少？我先查询现有接入")).toBe(true);
    });

    it("前缀词命中：请提供/请补充/请确认/请问/是否", () => {
        for (const t of [
            "请提供通信协议",
            "请补充转发地址",
            "请确认转发地址映射",
            "请问需要转发吗",
            "是否在已有实例上追加",
        ]) {
            expect(question_hit(t)).toBe(true);
        }
    });

    it("排除清单：方案确认句式不置位 questionPending（走按钮 arm 判定）", () => {
        expect(question_hit("接入方案摘要如下：是否确认执行？")).toBe(false);
        expect(question_hit("信息齐全，确认执行。")).toBe(false);
    });

    it("陈述中含「是否」不误报（句首锚定）", () => {
        expect(question_hit("下面说明端口是否必填。")).toBe(false);
    });

    it("普通陈述不命中", () => {
        expect(question_hit("接入方案已执行完成，10 个点已配置")).toBe(false);
        expect(question_hit("")).toBe(false);
    });
});

describe("check_tool_gate——工具前置条件表（§2.4.1）", () => {
    it("非闸门工具一律放行", () => {
        expect(check_tool_gate("query_abbr_registry", snap())).toBeNull();
        expect(check_tool_gate("csv_parser", snap({ phase: "executing" }))).toBeNull();
    });

    it("B1 回归：access_plan 前置为会话级 deviceInfo（跨回合复用），不再要求本回合", () => {
        expect(
            check_tool_gate("output_access_plan", snap({ phase: "planning", deviceInfoReady: true })),
        ).toBeNull();
        expect(
            check_tool_gate("output_access_plan", snap({ phase: "planning", deviceInfoReady: false })),
          ).toContain("output_device_info");
    });

    it("B2 回归：device_info 仅在 executing 被拒（planning/confirmed 打回重收集必须可达）", () => {
        expect(check_tool_gate("output_device_info", snap({ phase: "idle" }))).toBeNull();
        expect(check_tool_gate("output_device_info", snap({ phase: "planning" }))).toBeNull();
        expect(check_tool_gate("output_device_info", snap({ phase: "confirmed" }))).toBeNull();
        expect(check_tool_gate("output_device_info", snap({ phase: "executing" }))).toContain(
            "执行进行中",
        );
    });

    it("plan_steps：confirmed+userConfirmed 放行；未确认拒绝并引导按钮", () => {
        expect(
            check_tool_gate("output_plan_steps", snap({ phase: "confirmed", userConfirmed: true })),
        ).toBeNull();
        const refusal = check_tool_gate(
            "output_plan_steps",
            snap({ phase: "planning", userConfirmed: false }),
        );
        expect(refusal).toContain("确认按钮");
    });

    it("M6 回归：planning + userConfirmed（执行失败修复轮）放行 plan_steps", () => {
        expect(
            check_tool_gate("output_plan_steps", snap({ phase: "planning", userConfirmed: true })),
        ).toBeNull();
    });

    it("提问即终局：questionPending 对 access_plan / plan_steps 一票否决（优先级最高）", () => {
        for (const tool of ["output_access_plan", "output_plan_steps"]) {
            const refusal = check_tool_gate(
                tool,
                snap({
                    phase: "planning",
                    deviceInfoReady: true,
                    userConfirmed: true,
                    questionPending: true,
                }),
            );
            expect(refusal).toContain("提问即终局");
        }
    });
});
