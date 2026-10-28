// c4/agent/frontend/src/version.ts — 产品版本 → 周期表展示名（2026-10-28 用户裁定）
//
// 格式：MAJOR.MINOR.PATCH → C4{元素符号}{MINOR}.{PATCH}，元素序号 = MAJOR。
//   2.1.96 → C4He1.96（2 号元素 He）
//   3.5.127 → C4Li5.127（3 号元素 Li）
// 「跳过 C 元素」：6 号元素符号是 C（C4 前缀会成 C4C…）——MAJOR=6 时跳用
// 下一元素 N（7 号）表示。

const ELEMENT_SYMBOLS: readonly string[] = [
    "H", // 1
    "He", // 2
    "Li", // 3
    "Be", // 4
    "B", // 5
    "C", // 6（跳过——与 C4 前缀重复）
    "N", // 7
    "O", // 8
    "F", // 9
    "Ne", // 10
    "Na", // 11
    "Mg", // 12
    "Al", // 13
    "Si", // 14
    "P", // 15
    "S", // 16
    "Cl", // 17
    "Ar", // 18
    "K", // 19
    "Ca", // 20
];

/** MAJOR 序号 → 元素符号；C 跳用下一元素；超出表/非法回落数字本身 */
export function element_for_major(major: number): string {
    if (!Number.isInteger(major) || major < 1) {
        return String(major);
    }
    let idx = major - 1;
    if (ELEMENT_SYMBOLS[idx] === "C") {
        idx += 1; // 跳过 C 元素
    }
    return ELEMENT_SYMBOLS[idx] ?? String(major);
}

/** "2.1.96" → "C4He1.96"；非 MAJOR.MINOR.PATCH 形态原样返回 */
export function c4_version_label(version: string): string {
    const m = version.match(/^(\d+)\.(\d+)\.(\d+)$/);
    if (m === null) {
        return version;
    }
    return `C4${element_for_major(Number(m[1]))}${m[2]}.${m[3]}`;
}
