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

    it("数据来源场站前缀为异站完整名（site_change 72② 形态）→ other", () => {
        expect(
            deterministic_site_tag(
                "现在需要接入大唐辽宁三区风电场6号风机的数据，转发采用asfp2协议",
                "国电河北II区",
            ),
        ).toBe("other");
    });

    it("数据来源场站前缀为旧站名（site_change 72① 形态）→ other", () => {
        expect(
            deterministic_site_tag(
                "现在需要接入华能阿拉善6号风机的数据，使用端口9006",
                "国电河北II区",
            ),
        ).toBe("other");
    });

    it("数据来源场站前缀为绑定场站+通用后缀的超集泛化（71② 形态）→ 不拒绝", () => {
        expect(
            deterministic_site_tag(
                "现在需要接入大唐辽宁三区风电场3号风机的数据，使用端口9003",
                "大唐辽宁三区",
            ),
        ).toBeNull();
    });

    it("数据来源场站前缀为绑定场站去品牌子集（71③ 形态）→ 不在确定性层拒绝", () => {
        expect(
            deterministic_site_tag(
                "现在需要接入河北II区风电场4号风机的数据，使用端口9004",
                "国电河北II区",
            ),
        ).toBe("ambiguous");
    });

    it("数据来源场站前缀即绑定场站（语义等价）→ 交后续规则（不因前缀拒绝）", () => {
        expect(
            deterministic_site_tag("现在需要接入阿拉善6号风机的数据", "华能阿拉善"),
        ).toBe("ambiguous");
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

describe("arbitrate_site_tags（确定性标签优先，LLM 兜底）", () => {
    it("确定性 other → other（LLM 不得放行）", () => {
        expect(arbitrate_site_tags("other", null)).toBe("other");
        expect(arbitrate_site_tags("other", "ambiguous")).toBe("other");
        expect(arbitrate_site_tags("other", "ambiguous")).toBe("other");
    });

    it("确定性 ambiguous → ambiguous（LLM 抖动为 other 不得拖成拒绝，71③）", () => {
        expect(arbitrate_site_tags("ambiguous", null)).toBe("ambiguous");
        expect(arbitrate_site_tags("ambiguous", "other")).toBe("ambiguous");
    });

    it("确定性 null → LLM 标签兜底（纯语义形态）", () => {
        expect(arbitrate_site_tags(null, "other")).toBe("other");
        expect(arbitrate_site_tags(null, "ambiguous")).toBe("ambiguous");
    });

    it("双方均无标签 → null（放行）", () => {
        expect(arbitrate_site_tags(null, null)).toBeNull();
    });
});
