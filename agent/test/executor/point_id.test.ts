// c4/agent/test/executor/point_id.test.ts
// derive_point_id 单元测试（agent.md §3.2.1.3b：点名→英文标识，不自动生成；vitest）
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import { derive_point_id } from "../../src/executor/point_rules.js";

const IDENTIFIER_RE = /^[a-zA-Z][a-zA-Z0-9_]*$/;
const MAX_LEN = 1024;

describe("derive_point_id", () => {
    it("提供的英文标识优先（提取层翻译结果）", () => {
        const r = derive_point_id("windspeed", "风速", IDENTIFIER_RE, MAX_LEN);
        expect(r).toEqual({ id: "windspeed", error: null });
    });

    it("合规英文点名原样用作 id", () => {
        const r = derive_point_id("", "windspeed", IDENTIFIER_RE, MAX_LEN);
        expect(r).toEqual({ id: "windspeed", error: null });
    });

    it("中文点名且无英文标识 → 报错，不自动生成", () => {
        const r = derive_point_id("", "风速", IDENTIFIER_RE, MAX_LEN);
        expect(r.id).toBe("");
        expect(r.error).toContain("不自动生成");
    });

    it("未命名（两侧皆空）→ 报错，交由上游追问", () => {
        const r = derive_point_id("", "", IDENTIFIER_RE, MAX_LEN);
        expect(r.id).toBe("");
        expect(r.error).not.toBeNull();
    });

    it("非规范点名（数字开头/含连字符）→ 报错", () => {
        for (const bad of ["1风速", "wind-speed", "点1000"]) {
            const r = derive_point_id("", bad, IDENTIFIER_RE, MAX_LEN);
            expect(r.id).toBe("");
            expect(r.error).not.toBeNull();
        }
    });

    it("点名超长 → 专属报错", () => {
        const r = derive_point_id("", "x".repeat(1025), IDENTIFIER_RE, 1024);
        expect(r.id).toBe("");
        expect(r.error).toContain("1024");
    });

    it("提供的英文标识经 trim 后生效", () => {
        const r = derive_point_id("  windspeed  ", "", IDENTIFIER_RE, MAX_LEN);
        expect(r).toEqual({ id: "windspeed", error: null });
    });
});
