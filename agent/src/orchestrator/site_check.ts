// c4/agent/src/orchestrator/site_check.ts — 场站归属判定纯函数
// agent.md「site 获取机制」（归属三态）与「场地判定仲裁规则」：阶段 1 的
// location_prompt LLM 语义判定与确定性地名比对并行执行，取更保守一方。
// 2026-10-18 增区序号层（用户口径：场站名 = 集团+风场+区?，区为可选尾部序号，
// 如 {三峡风电}+{宜昌}+{2区}、{大唐辽宁}+{小塔子光伏}+{III区}；亦有无区场站
// 如 {华能蒙东}+{高立板}）：消息接入侧子句的源区序号与场站尾区序号可确定性
// 比对——不同 → other（拒绝），相同 → match（确定性通过，免问免掷币）。
// 一/1/I、二/2/II、三/3/III 同义归一；「转发到X区」属转发目标子句、「点表与
// X区一致」属点表参照子句，均不构成源区声明（不参与比对）。

export type SiteCheckTag = "ambiguous" | "other" | "match" | null;

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
    // 区序号确定性裁决（放在既有规则之后：异站品牌/地名命中优先于区相同——
    // 「接入大唐…光伏2区3号风机」对「三峡…2区」区虽同但品牌异站，仍 other）
    const sZone = site_zone_of(siteName);
    if (sZone !== null) {
        const src = source_zone_ordinals(text);
        if (src.length > 0) {
            return src.every((z) => z === sZone) ? "match" : "other";
        }
    }
    const loc = siteName.slice(2);
    if (loc.length >= 2 && text.includes(loc)) return "ambiguous";
    if (text.includes(siteName.slice(0, 2))) return "other";
    return null;
}

// ── 区序号归一化（集团+风场+区? 口径）──────────────────────

const ZONE_ORD_RE =
    /([0-9]{1,3}|[零一二两三四五六七八九十]{1,3}|[IVXivx]{1,5}|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]{1,4})区/g;

const ZH_DIGIT: Record<string, number> = {
    零: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9,
};
const ROMAN_DIGIT: Record<string, number> = {
    I: 1, V: 5, X: 10, "Ⅰ": 1, "Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "Ⅴ": 5,
    "Ⅵ": 6, "Ⅶ": 7, "Ⅷ": 8, "Ⅸ": 9, "Ⅹ": 10,
};

/** 区序号 token 归一化（一/两→2、I/Ⅰ→1、II/Ⅱ→2、III→3…；非法返回 null） */
export function zone_value(token: string): number | null {
    if (/^[0-9]{1,3}$/.test(token)) return Number(token);
    if (/^[零一二两三四五六七八九十]+$/.test(token)) {
        if (token.includes("十")) {
            const [head, tail] = token.split("十");
            const h = head === "" ? 1 : (ZH_DIGIT[head] ?? NaN);
            const t = tail === "" ? 0 : (ZH_DIGIT[tail] ?? NaN);
            const v = h * 10 + t;
            return Number.isFinite(v) ? v : null;
        }
        return ZH_DIGIT[token] ?? null;
    }
    if (/^[IVXivx]+$/.test(token) || /^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+$/.test(token)) {
        const chars = [...token.toUpperCase()].map((c) => ROMAN_DIGIT[c] ?? NaN);
        if (chars.some((v) => !Number.isFinite(v))) return null;
        // 标准罗马读法（含减文 IV=4/IX=9）
        let sum = 0;
        for (let i = 0; i < chars.length; i++) {
            const cur = chars[i]!;
            const next = i + 1 < chars.length ? chars[i + 1]! : 0;
            sum += cur < next ? -cur : cur;
        }
        return sum;
    }
    return null;
}

/** 场站名尾区序号（三峡风电宜昌2区→2、大唐辽宁小塔子光伏III区→3、无区→null） */
export function site_zone_of(siteName: string): number | null {
    const tokens = [...siteName.matchAll(ZONE_ORD_RE)];
    if (tokens.length === 0) return null;
    const last = tokens[tokens.length - 1]!;
    return last.index + last[0].length === siteName.length
        ? zone_value(last[1])
        : null;
}

/** 消息接入侧源区序号：排除转发目标子句（「转发到X区」）、点表参照子句
 * （「点表与X区一致」）与风场名词子句（「河北II区风电场」——区词是场站名
 * 组成部分而非裸区声明，71③ 去品牌子集确认口径不受影响）。这两类/三类
 * 子句的区词都不是场站声明 */
export function source_zone_ordinals(message: string): number[] {
    const out: number[] = [];
    const clauses = message
        .split(/[，,。；;;\n]+/)
        .map((c) => c.trim())
        .filter((c) => c.length > 0);
    for (const clause of clauses) {
        // 转发目标子句排除含口语动词形态（转到/转往/发到/发往/送到/送至——
        // 2026-10-27 生产实测：「转到2区，asfp2协议，127.0.0.1:9900」的「转到」
        // 不命中「转发」，2区被当源区声明，与 1区绑定场站确定性冲突误拒接入）
        if (/转发|转到|转往|发到|发往|送到|送至|入库|写入|推送|发送|目标|服务器/.test(clause)) continue;
        if (/点表|一致/.test(clause)) continue;
        if (/风电场|风场|光伏|电站|场站/.test(clause)) continue;
        for (const m of clause.matchAll(ZONE_ORD_RE)) {
            const v = zone_value(m[1]);
            if (v !== null && !out.includes(v)) out.push(v);
        }
    }
    return out;
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
 * "match"（区序号确定性同区）为最优先——确定性放行压过 LLM 保守追问。
 */
export function arbitrate_site_tags(det: SiteCheckTag, llm: SiteCheckTag): SiteCheckTag {
    if (det !== null) return det;
    return llm;
}
