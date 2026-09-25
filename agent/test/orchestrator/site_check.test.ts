// c4/agent/test/orchestrator/site_check.test.ts
// 场站归属判定纯函数单测（agent.md「site 获取机制」+「场地判定仲裁规则」；
// func_test_case 用例 3 回归：判定输入必须是消息原文而非设备名）
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import {
    arbitrate_site_tags,
    deterministic_site_tag,
    llm_site_tag,
} from "../../src/orchestrator/site_check.js";

describe("deterministic_site_tag", () => {
    const site = "华能阿拉善";

    it("消息含完整场站名（一致场站，用例 2 形态）→ null", () => {
        expect(
            deterministic_site_tag("现在需要接入华能阿拉善风电场1号风机的数据", site),
        ).toBeNull();
    });

    it("消息无任何场站信息（用例 1 形态）→ null", () => {
        expect(
            deterministic_site_tag("现在需要接入1号风机的数据，asfp2协议", site),
        ).toBeNull();
    });

    it("消息含他站完整场站名（用例 3 形态，设备名提取剥前缀后仍须命中）→ other", () => {
        expect(
            deterministic_site_tag("现在需要接入华能通辽风电场1号风机的数据", site),
        ).toBe("other");
    });

    it("地名一致但非完整场站名 → ambiguous", () => {
        expect(deterministic_site_tag("接入阿拉善风电场的数据", site)).toBe("ambiguous");
    });

    it("空场站名（未绑定）→ null", () => {
        expect(deterministic_site_tag("任意文本", "")).toBeNull();
    });
});

describe("llm_site_tag", () => {
    it("rule4 明确冲突 → other", () => {
        expect(
            llm_site_tag({
                mode: "check",
                is_consistent: false,
                matched_rule: "rule4",
                reason: "不同地名",
            }),
        ).toBe("other");
    });

    it("rule5 模糊/保守不一致 → ambiguous", () => {
        expect(
            llm_site_tag({
                mode: "check",
                is_consistent: false,
                matched_rule: "rule5",
                reason: "缺少分区信息",
            }),
        ).toBe("ambiguous");
    });

    it("判定一致 → null", () => {
        expect(
            llm_site_tag({ mode: "check", is_consistent: true, matched_rule: "rule2" }),
        ).toBeNull();
    });

    it("首接入模式 → null（绑定不走归属拒绝）", () => {
        expect(
            llm_site_tag({
                mode: "first_access",
                is_consistent: true,
                matched_rule: "rule6",
                user_site: "华能阿拉善",
                generated_abbr: "hnals",
            }),
        ).toBeNull();
    });

    it("解析失败 / 字段缺失 → null（交由确定性层兜底）", () => {
        expect(llm_site_tag(null)).toBeNull();
        expect(llm_site_tag({})).toBeNull();
        expect(llm_site_tag({ mode: "check", is_consistent: "false" })).toBeNull();
    });
});

describe("arbitrate_site_tags（取更保守方）", () => {
    it("任一方 other → other", () => {
        expect(arbitrate_site_tags("other", null)).toBe("other");
        expect(arbitrate_site_tags(null, "other")).toBe("other");
        expect(arbitrate_site_tags("other", "ambiguous")).toBe("other");
    });

    it("任一方 ambiguous 且无 other → ambiguous", () => {
        expect(arbitrate_site_tags("ambiguous", null)).toBe("ambiguous");
        expect(arbitrate_site_tags(null, "ambiguous")).toBe("ambiguous");
    });

    it("LLM 判一致但确定性有标签 → 以确定性为准（agent.md 935）", () => {
        expect(arbitrate_site_tags("ambiguous", null)).toBe("ambiguous");
        expect(arbitrate_site_tags("other", null)).toBe("other");
    });

    it("双方均无标签 → null（放行）", () => {
        expect(arbitrate_site_tags(null, null)).toBeNull();
    });
});
