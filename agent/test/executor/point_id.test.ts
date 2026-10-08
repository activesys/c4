// c4/agent/test/executor/point_id.test.ts
// derive_point_id 单元测试（agent.md §3.2.1.3b 2026-10-07 裁定：id＝点名确定性
// 归一化，翻译退役；显式 id 须为安全 key；vitest）
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

    it("中文点名归一化作裸 id（2026-10-07 裁定：不再报错/翻译）", () => {
        const r = derive_point_id("", "风速", IDENTIFIER_RE, MAX_LEN);
        expect(r).toEqual({ id: "风速", error: null });
    });

    it("未命名（两侧皆空）→ 报错，交由上游追问", () => {
        const r = derive_point_id("", "", IDENTIFIER_RE, MAX_LEN);
        expect(r.id).toBe("");
        expect(r.error).not.toBeNull();
    });

    it("数字开头/连字符在白名单内 → 合法 key；纯符号 → 报错追问", () => {
        expect(derive_point_id("", "1风速", IDENTIFIER_RE, MAX_LEN).id).toBe("1风速");
        expect(derive_point_id("", "wind-speed", IDENTIFIER_RE, MAX_LEN).id).toBe("wind-speed");
        expect(derive_point_id("", "点1000", IDENTIFIER_RE, MAX_LEN).id).toBe("点1000");
        const bad = derive_point_id("", "·。·", IDENTIFIER_RE, MAX_LEN);
        expect(bad.id).toBe("");
        expect(bad.error).toContain("请提供点名");
    });

    it("点名超长 → 归一化截断至字节预算（960B），不报错", () => {
        const r = derive_point_id("", "x".repeat(1025), IDENTIFIER_RE, 1024);
        expect(r.error).toBeNull();
        expect(r.id.length).toBeLessThanOrEqual(960);
    });
    it("显式 id 携带点号/空白 → 安全校验报错", () => {
        const r = derive_point_id("a.b", "风速", IDENTIFIER_RE, MAX_LEN);
        expect(r.id).toBe("");
        expect(r.error).toContain(".");
    });

    it("提供的英文标识经 trim 后生效", () => {
        const r = derive_point_id("  windspeed  ", "", IDENTIFIER_RE, MAX_LEN);
        expect(r).toEqual({ id: "windspeed", error: null });
    });
});
