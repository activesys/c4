// c4/agent/src/registry/device_prefix.ts — 设备前缀确定性派生（agent.md §3.2.1.3c）
// 类型映射表（风机→wt、主变→zy、逆变器→nb、测风塔→cft、光伏→gf、储能→cn、
// 升压站→syz——2026-10-01 补，原表缺失此条目曾落 dev 兜底）+ 名称中的编号；
// 编号支持中文数字（三号 → 3，复用 zh_numeral）。单台无编号 → 纯类型缩写
// （升压站 → syz，点 key 如 syz_active_power）；表未命中 → 拼音首字母/ASCII；
// 完全匿名（用户明确「没有名字」）→ "dev" 基名，调用方按注册表分配 dev{N} 序列。
// 供 orchestrator（方案层）与 output_plan_steps（拆解器）共同使用。

import { zh_convert } from "../orchestrator/zh_numeral.js";

const DEVICE_TYPE_PREFIX: Array<[RegExp, string]> = [
    [/风机|风电机组/, "wt"],
    [/主变|变压器/, "zy"],
    [/逆变器/, "nb"],
    [/测风塔/, "cft"],
    [/光伏/, "gf"],
    [/储能/, "cn"],
    [/升压站/, "syz"],
];

function device_number(name: string): string {
    const ar = name.match(/(\d+)\s*[#号]?/);
    if (ar) return ar[1];
    const zh = name.match(/([零一二两三四五六七八九十]{1,6})\s*[#号]/);
    if (zh) {
        const n = zh_convert(zh[1]);
        if (n !== null) return String(n);
    }
    return "";
}

/** 设备名 → 候选前缀（确定性，注册表撞名后由 resolve_prefix_conflict 顺延）。 */
export function device_prefix_candidate(name: string): string {
    const num = device_number(name);
    for (const [re, pre] of DEVICE_TYPE_PREFIX) {
        if (re.test(name)) return num !== "" ? `${pre}${num}` : pre;
    }
    // 前缀不含下划线（§3.2.1.3c）：注册表重建/设备归属按点 key 首个 `_` 切分，
    // 前缀带 `_` 会破坏该假设——power_forecast → powerforecast
    const ascii = name.replace(/[^a-zA-Z0-9]/g, "");
    if (ascii.length > 0 && /^[a-zA-Z]/.test(ascii)) return ascii.toLowerCase();
    return num !== "" ? `dev${num}` : "dev";
}
