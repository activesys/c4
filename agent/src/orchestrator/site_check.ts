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
 * 仲裁（agent.md：确定性标签优先；冲突不静默吞并，取更保守的一方）。
 * 保守序：other（拒绝）> ambiguous（追问确认）> null（放行）。
 */
export function arbitrate_site_tags(det: SiteCheckTag, llm: SiteCheckTag): SiteCheckTag {
    if (det === "other" || llm === "other") return "other";
    if (det === "ambiguous" || llm === "ambiguous") return "ambiguous";
    return null;
}
