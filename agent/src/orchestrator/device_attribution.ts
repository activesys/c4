// c4/agent/src/orchestrator/device_attribution.ts — 设备名提取纯函数层
// 2026-10-19 用户裁定：设备类型为封闭集合（13 类）——风机、升压站、电计量、
// 计量小站、风功率、测风塔、光伏、箱变、逆变器、汇流箱、故障录波、保信、
// 气象数据。设备识别以词表为锚点。
// 2026-10-18 生产野测事故：「接入的应该是2区…」——「接入X的数据」懒惰正则把
// 「的应该是2区」整体绑为设备名。裁定：开头连接词/系词剥离 + 词表锚定 + 剥后
// 无类型词视为未给出设备名（宁可追问不可错绑，47① 同口径）。
//
// clean_device_candidate 规则：
//   ① 迭代剥离开头连接词/系词（的/是/了/应该/就是/使用）——只剥语法垃圾类，
//     和/与/在/为 等可能是真实地名首字（和田风电场）不剥；
//   ② 剥后为空 → null（追问）；
//   ③ 含词表类型词 → 有效（保留编号+类型+后缀全名：「1号风机变桨控制器」
//     「2区箱变」「14区1#逆变器」）；
//   ④ 无类型词但剥过开头词 = 捕获本身是语法片段（「的应该是2区」）→ null（追问）；
//   ⑤ 无类型词且捕获干净 → 原样保留（词表是锚点不是排他白名单：词表外真实
//     设备名合法——「海林华锐」「power_forecast_1」「1号主变测控装置」，MB-01/
//     用例 34 依赖此路径）。

/** 设备类型封闭词表（2026-10-19 用户给定，目前设备就这些） */
export const DEVICE_TYPE_WORDS: readonly string[] = [
    "风机",
    "升压站",
    "电计量",
    "计量小站",
    "风功率",
    "测风塔",
    "光伏",
    "箱变",
    "逆变器",
    "汇流箱",
    "故障录波",
    "保信",
    "气象数据",
];

/** 开头语法垃圾词（连接词/系词/引导词）：只收事故句式实证的词类，见头注① */
const LEADING_FILLER_RE = /^(?:应该|就是|使用|的|是|了)+/;

/** 候选名含词表类型词时返回该类型词，否则 null */
export function device_type_of(span: string): string | null {
    for (const word of DEVICE_TYPE_WORDS) {
        if (span.includes(word)) {
            return word;
        }
    }
    return null;
}

/**
 * 设备名候选清洗：开头垃圾剥离 + 词表锚定（规则①~⑤见头注）。
 * 返回 null = 未给出可绑定的设备名，调用方走缺口追问。
 */
export function clean_device_candidate(raw: string): string | null {
    const original = raw.trim();
    if (original === "") {
        return null;
    }
    const hadFiller = LEADING_FILLER_RE.test(original);
    let name = original;
    for (let prev = ""; prev !== name; ) {
        prev = name;
        name = name.replace(LEADING_FILLER_RE, "");
    }
    if (name === "") {
        return null;
    }
    if (device_type_of(name) !== null) {
        return name;
    }
    if (hadFiller) {
        return null;
    }
    return name;
}

/** 场站语汇标记（引导段判定）：含任一即视为场站指称（71①/53 前导剥离用） */
const SITE_LEAD_MARK_RE = /场|站|区|集团|公司|能源|电力|风电|光伏|风场/;

/**
 * 设备名候选的场站语境剥离（2026-10-19 用例 71①/extra 53 实测）：
 * 「接入国电河北II区2号风机的数据」「接入阿拉善风电场2号风机的数据」——场站
 * 指称被 acc 全跨度捕获进设备名，2.1.86 名原样体系下点 key 成了
 * 「国电河北ii区2号风机_风速」。场站是归属语境不是设备身份，两层剥离：
 *   ① 候选以已绑场站全名开头（忽略大小写）→ 剥全名前缀；
 *   ② 尾段恰为「编号+类型词」整体、且前导含场站语汇（场/站/区/集团/风电…）
 *      → 前导视为场站指称剥离（阿拉善风电场2号风机 → 2号风机）。
 * 类型词链不受影响（「1号风机变桨控制器」尾段非编号类型词整体）；去品牌/缺区号
 * 的部分匹配交场站判定流程（71③/④ 口径），本函数不猜。
 */
export function strip_site_prefix(candidate: string, siteName: string | null): string {
    let out = candidate.trim();
    if (out === "") {
        return out;
    }
    // ① 已绑场站全名前缀（忽略大小写）——剥完继续走 ②（71② 实测：「国电河北
    //    II区风电场3号风机」①剥全名后剩「风电场3号风机」，早返回漏掉②）
    if (siteName !== null) {
        const site = siteName.trim();
        if (site.length >= 2 && out.length > site.length &&
            out.toLowerCase().startsWith(site.toLowerCase())) {
            out = out.slice(site.length);
        }
    }
    // ② 尾段恰为「编号+类型词」整体、前导含场站语汇 → 剥前导（阿拉善风电场2号
    //    风机 → 2号风机）；候选整体即单一编号设备指称（14区1#逆变器）不剥
    const m = out.match(NUM_DEVICE_TAIL_RE);
    if (m !== null && SITE_LEAD_MARK_RE.test(m[1]!) &&
        NUM_DEVICE_FULL_RE.test(out) === false) {
        out = out.slice(out.length - m[2]!.length);
    }
    return out;
}

/** 47① 纯类型词口径（不得以类型词蒙混、无编号即追问）：历史词 ∪ 词表（升压站
 *  除外——单台无编号设备按类型词指称即合法设备名，§3.2.1.3c 既有例外）。
 *  锚定 ^...$：只拦「接入逆变器的数据」这类裸类型指称，不伤全名。 */
export const TYPE_ONLY_DEVICE_RE = new RegExp(
    "^(?:" +
        [...new Set(["风机", "风电机组", "主变", "变压器", "逆变器", "测风塔", "机组", "光伏", "储能", "数据源", "设备", ...DEVICE_TYPE_WORDS])]
            .filter((word) => word !== "升压站")
            .sort((a, b) => b.length - a.length)
            .join("|") +
        ")$",
);

// ── 编号设备扫描（2026-10-19 用户口径：编号也是设备名的一部分）──────────
// 「1#风机」「2号变压器」「箱变123」「二十三区光伏」「14区1#逆变器」——前缀
// 限定（数字/中文数字 + #/号/区，可叠多段）与后缀编号（类型词后跟数字）都算
// 设备指称，整体返回。裸类型词（无前缀限定且无后缀编号）不算——交 47① 追问。
// 词表外历史类型词（主变/变压器/机组/风电机组/数据源）一并可扫（2号变压器
// 是用户给定示例；「1号主变测控装置」长名仍由 acc/dm 全名捕获路径负责，本扫
// 描只作 nm 回退，命中类型词即止——后缀不吞类型词后续接的非数字字符）。

const DEVICE_MENTION_TYPES = [
    "气象数据",
    "故障录波",
    "计量小站",
    "风电机组",
    "电计量",
    "风功率",
    "测风塔",
    "升压站",
    "数据源",
    "逆变器",
    "汇流箱",
    "变压器",
    "光伏",
    "机组",
    "主变",
    "风机",
    "箱变",
    "保信",
].sort((a, b) => b.length - a.length);

const QUAL_UNIT = "(?:[0-9零一二两三四五六七八九十百]{1,6}\\s*[#号区])";
const NUM_DEVICE_RE = new RegExp(
    `((?:${QUAL_UNIT}\\s*){1,3}(?:${DEVICE_MENTION_TYPES.join("|")})` +
        `|(?:${DEVICE_MENTION_TYPES.join("|")})\\s*[0-9]{1,8}[A-Za-z]?)`,
);

/**
 * 编号设备指称扫描：前缀限定（1#/2号/二十三区/14区1#…）+ 类型词，或类型词 +
 * 后缀数字（箱变123）。命中返回去空格后的完整指称；裸类型词/无设备指称返回
 * null（调用方交 47① 追问）。
 */
export function numbered_device_of(semantic: string): string | null {
    const m = semantic.match(NUM_DEVICE_RE);
    return m ? m[1].replace(/\s+/g, "") : null;
}

/** 尾段锚定的「编号+类型词」整体（前导场站指称截离用；复用编号扫描口径） */
const NUM_DEVICE_TAIL_RE = new RegExp(
    `^(.{2,}?)((?:${QUAL_UNIT}\\s*){1,3}(?:${DEVICE_MENTION_TYPES.join("|")}))$`,
);

/** 候选整体即单一编号设备指称（多段限定词堆叠，如「14区1#逆变器」「二十三区
 *  光伏」——用户给定合法全名形态）：整体命中时不做前导截离 */
const NUM_DEVICE_FULL_RE = new RegExp(
    `^(?:(?:${QUAL_UNIT}\\s*){1,3})(?:${DEVICE_MENTION_TYPES.join("|")})$`,
);
