// 资产键归一化 alias_key_norm（c4_asset_learning.md §7.1）。
// 不直接复用 normalize_point_name——其不折叠中文数字（「一号主变」与「1#主变」
// 仍是两个键）。本规范 = normalize_point_name 折叠 + 编号语境中文数字折叠 +
// 分隔符等价类，使「一号主变」≡「1#主变」≡「1号主变」。
//
// 中文数字折叠只在编号语境触发（§7.1，第三轮评审 F1 修订）：zh_numeral 是
// 「从 X 开始/起」句式提取器而非通用折叠工具，逐字替换会把「十一号」错折、
// 把「一致」「统一」「二次回路」等普通词误折——规则：
//   (a) 触发条件：数字串紧邻数字/#/编号量词白名单成员，或整串为纯中文数字；
//   (b) 组合数字按进位求值（zh_convert：十一=11、一百二十=120），不逐字替换；
//   (c) 其余语境一律不折叠。
// 规则版本随 asset_schema_version 记录；变更须全量重算现存键。

import { normalize_point_name, zh_convert } from "@c4/stages";

/** 编号量词白名单（封闭常量表——「次」「回」等通用量词不在清单内，G1） */
const NUMBERED_QUANTIFIERS: ReadonlyArray<string> = ["号", "号机", "号机组", "台"];

/** 中文数字串（含进位字与万；〇/零计入） */
const CN_NUMERAL_RUN = /[零〇一二两三四五六七八九十百千万]+/g;

function is_digit_like(ch: string | undefined): boolean {
    return ch !== undefined && /[0-9#_]/.test(ch);
}

function next_starts_numbered_quantifier(s: string, pos: number): boolean {
    return NUMBERED_QUANTIFIERS.some((q) => s.startsWith(q, pos));
}

/**
 * 资产键归一化。空串输入返回空串；折叠失败的中文数字串原样保留（不猜测）。
 */
export function alias_key_norm(input: string): string {
    let s = normalize_point_name(input);
    if (s === "") return "";
    // 分隔符等价类（§7.1）：# 与 号 等价、- 与 _ 等价
    s = s.replace(/#/g, "号").replace(/[-—–]/g, "_");
    // 编号语境中文数字折叠
    s = s.replace(CN_NUMERAL_RUN, (run: string, offset: number, full: string): string => {
        const prev = offset > 0 ? full[offset - 1] : undefined;
        const nextPos = offset + run.length;
        const next = full[nextPos];
        const wholeToken =
            (offset === 0 || /[_\s]/.test(full[offset - 1] ?? "_")) &&
            (nextPos >= full.length || /[_\s]/.test(full[nextPos] ?? "_"));
        const triggered =
            wholeToken ||
            is_digit_like(prev) ||
            is_digit_like(next) ||
            next_starts_numbered_quantifier(full, nextPos);
        if (!triggered) return run;
        const n = zh_convert(run);
        return n === null ? run : String(n);
    });
    return s;
}
