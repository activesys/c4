// c4/agent/src/orchestrator/site_check.ts — 场站归属判定纯函数
// agent.md「site 获取机制」（归属三态）与「场地判定仲裁规则」：阶段 1 的
// location_prompt LLM 语义判定与确定性地名比对并行执行，取更保守一方。

export type SiteCheckTag = "ambiguous" | "other" | null;

/**
 * 确定性地名比对（agent.md：用户资料无场站信息→默认本站；地名一致但非完整
 * 场站名→ambiguous；场站前缀+其他地名→other）。输入必须是消息原文 semantic——
 * 设备名提取会剥掉场站前缀（「华能通辽风电场1号风机」→「1号风机」），用它
 * 比对会漏判（func_test_case 用例 3 回归根因）。
 */
export function deterministic_site_tag(text: string, siteName: string): SiteCheckTag {
    if (!siteName) return null;
    if (text.includes(siteName)) return null;
    // 数据来源场站前缀（location_prompt 提取范围 3：设备编号前的地名前缀是数据
    // 来源场站，如「接入华能阿拉善6号风机的数据」中的「华能阿拉善」）——完整
    // 前缀与绑定场站无包含关系 → other。转确定性判定的原因：语义层在 glm-4.6v
    // 下对「接入{异站完整名}{N}号风机」形态漏判（site_change 链步 72② 实测
    // 2026-10-10），该形态机构上可判定，不依赖模型
    const srcSite = text.match(/接入([^\s，。,，；;]+?)\d+号(?:风机|机组)/);
    if (srcSite) {
        // 候选剥通用后缀再比对：{绑定场站}+「风电场」的超集泛化（71②）与去品牌
        // 子集（71③，交 ambiguous 确认）都不是异站；真异站（72①/72②，品牌地名
        // 均无交集）才 other
        const cand = srcSite[1].trim().replace(/(风电场|风场|光伏电站|光伏|电站|场站|风电)+$/, "");
        if (
            cand.length >= 2 && cand !== siteName &&
            !siteName.includes(cand) && !cand.includes(siteName)
        ) {
            return "other";
        }
    }
    const loc = siteName.slice(2);
    if (loc.length >= 2 && text.includes(loc)) return "ambiguous";
    if (text.includes(siteName.slice(0, 2))) return "other";
    return null;
}

/**
 * location_prompt 输出 → 标签。rule4 场站明确冲突→other；rule5 模糊/无法
 * 确认（保守原则）→ambiguous；判定一致、首接入模式、解析失败→null
 * （null 交由确定性层与仲裁兜底）。
 */
export function llm_site_tag(parsed: Record<string, unknown> | null): SiteCheckTag {
    if (!parsed || parsed["mode"] === "first_access") return null;
    if (parsed["is_consistent"] !== false) return null;
    return parsed["matched_rule"] === "rule5" ? "ambiguous" : "other";
}

/**
 * 仲裁（agent.md「场地判定仲裁规则」：确定性标签优先——确定性层非空即生效，
 * LLM 判定仅在其为 null 时兜底）。依据：确定性比对机构上可复现，LLM 语义判定
 * 存在跨调用抖动（glm-4.6v 对去品牌形态在 rule4/rule5 间漂移，site_change 71③
 * 2026-10-10 实测：两层均 ambiguous 仍被单次 rule4 拖成 other 误拒）。
 * 保守序（LLM 兜底路径内）：other（拒绝）> ambiguous（追问确认）> null（放行）。
 */
export function arbitrate_site_tags(det: SiteCheckTag, llm: SiteCheckTag): SiteCheckTag {
    if (det !== null) return det;
    return llm;
}
