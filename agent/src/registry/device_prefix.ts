// c4/agent/src/registry/device_prefix.ts — 设备前缀派生（agent.md §3.2.1.3c）
// 2026-10-19 用户裁定（命名规范切换）：点 key 前缀 = **设备名原样归一化**，
// 类型缩写+编号体系（wt1/zy1/nbA01/syz，2026-10-01 建）退役——设备「1#风机」
// 点名「风速」→ 点 key「1#风机_风速」；升压站裸类型词 → 「升压站_有功功率」。
//   · 归一化走 normalize_point_name 同一管线：白名单符号（#、()、-、~ 等）保留，
//     `.`/空白/引号/等号转 `_`，ASCII 小写——key 机器安全由归一化层统一保证；
//   · 完全匿名（用户明确「没有名字」）→ "dev" 基名，调用方按注册表分配 dev{N} 序列；
//   · **同名设备不顺延**（2026-10-19 裁定）：派生前缀已被注册表占用 = 设备名与
//     既有设备相同，属点表/声明问题，调用方以可读理由拒收
//     （orchestrator prefix_taken_issue），不再 wt1→wt2 式改写。
// 供 orchestrator（方案层）、output_plan_steps（拆解器）、group_access（组模式）共用。

import { normalize_point_name } from "../executor/point_key.js";

/** 已知设备类型词（组模式类型词门）：13 类封闭词表 + 历史类型词 */
const KNOWN_TYPE_RE =
    /^(?:风机|风电机组|主变|变压器|逆变器|测风塔|机组|光伏|储能|数据源|设备|升压站|电计量|计量小站|风功率|箱变|汇流箱|故障录波|保信|气象数据)$/;

/**
 * 设备名 → 点 key 前缀：设备名原样归一化（「1#风机」→「1#风机」）。
 * 归一化后为空（纯符号/空白）→ "dev" 基名（匿名设备，注册表分配 dev{N}）。
 */
export function device_prefix_candidate(name: string): string {
    const normalized = normalize_point_name(name);
    return normalized !== "" ? normalized : "dev";
}

/** 类型词是否可识别（组模式门：未命中 → 组级缺口，不猜）。 */
export function device_type_known(typeWord: string): boolean {
    return KNOWN_TYPE_RE.test(typeWord);
}

/**
 * 组成员前缀（§2.11）：成员设备名（memberNames，如「1号风机」「A01逆变器」）
 * 原样归一化为前缀。类型词不可识别 → null（交调用方按组级缺口处理，不猜）。
 */
export function group_member_prefix(typeWord: string, memberName: string): string | null {
    if (!device_type_known(typeWord)) return null;
    return device_prefix_candidate(memberName);
}
