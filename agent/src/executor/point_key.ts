// @file 点 key 安全归一化（agent.md §3.2.1.3b，2026-10-07/08 裁定）
//
// 采集点 id 不再经 LLM 翻译，直接由点表原名归一化而来（确定性、可复现；C4_RS_00204
// 达标路径——接入管道零 LLM 翻译）。归一化保证 key 的机器安全：全局 key 以 `.` 分隔
// （{实例id}.{点key}），reader key 反解析（executor/orchestrator 共 7 处）与 shm 唯一键
// `serviceID + "." + pointID` 拼接都要求 key 内无 `.`；引号/逗号/等号/空白一并排除。

/** 点 key 字节上限（与 executor MAX_IDENTIFIER_LENGTH 同口径，UTF-8 字节） */
export const MAX_POINT_KEY_BYTES = 1024;

/** 归一化源上限（预留设备前缀与顺延后缀余量） */
const KEY_BYTE_BUDGET = 960;

/** 全角 → 半角折叠表（减少视觉同形导致的假性重复，如 １＃ vs 1#） */
const FULLWIDTH_FOLD: Readonly<Record<string, string>> = {
    "！": "!", "＂": "\"", "＃": "#", "％": "%", "＆": "&", "＇": "'",
    "（": "(", "）": ")", "＊": "*", "＋": "+", "，": ",", "－": "-", "．": ".",
    "／": "/", "：": ":", "；": ";", "＜": "<", "＝": "=", "＞": ">", "？": "?",
    "＠": "@", "［": "[", "＼": "\\", "］": "]", "＾": "^", "＿": "_",
    "｀": "`", "｛": "{", "｜": "|", "｝": "}", "～": "~",
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "Ａ": "A", "Ｂ": "B", "Ｃ": "C", "Ｄ": "D", "Ｅ": "E", "Ｆ": "F", "Ｇ": "G",
    "Ｈ": "H", "Ｉ": "I", "Ｊ": "J", "Ｋ": "K", "Ｌ": "L", "Ｍ": "M", "Ｎ": "N",
    "Ｏ": "O", "Ｐ": "P", "Ｑ": "Q", "Ｒ": "R", "Ｓ": "S", "Ｔ": "T", "Ｕ": "U",
    "Ｖ": "V", "Ｗ": "W", "Ｘ": "X", "Ｙ": "Y", "Ｚ": "Z",
    "ａ": "a", "ｂ": "b", "ｃ": "c", "ｄ": "d", "ｅ": "e", "ｆ": "f", "ｇ": "g",
    "ｈ": "h", "ｉ": "i", "ｊ": "j", "ｋ": "k", "ｌ": "l", "ｍ": "m", "ｎ": "n",
    "ｏ": "o", "ｐ": "p", "ｑ": "q", "ｒ": "r", "ｓ": "s", "ｔ": "t", "ｕ": "u",
    "ｖ": "v", "ｗ": "w", "ｘ": "x", "ｙ": "y", "ｚ": "z",
};

/** 白名单符号集（design §3.2.1.3b）：Unicode 字母/数字之外仅放行这些符号，
 *  其余（`.`、空白、`,``=`、引号、其余一切符号）归一化时替换为 `_` */
const WHITELIST_SYMBOLS = new Set(
    ["_", "#", "(", ")", "[", "]", "{", "}", "-", "~", "+", "!", "@", "^", "&", "%", "$", "*", "?"],
);

export function key_byte_length(s: string): number {
    return new TextEncoder().encode(s).length;
}

/**
 * 点表原名（可含设备语境前缀，如 `1#逆变器_有功功率`）→ 安全点 key：
 * NFC → 全半角折叠 → 白名单过滤（字母/数字/白名单符号保留，其余→`_`）→
 * 收拢/修剪 `_` → ASCII 小写 → 按码点截断至字节预算 → 复修剪尾部 `_`。
 * 返回空串 = 原名无可提取语义（纯符号/空白）——调用方按空名追问（不猜，fail-visible）。
 */
export function normalize_point_name(raw: string): string {
    const s0 = String(raw ?? "").normalize("NFC");
    let folded = "";
    for (const ch of s0) {
        folded += FULLWIDTH_FOLD[ch] ?? ch;
    }
    let out = "";
    for (const ch of folded) {
        if (/\p{L}|\p{N}/u.test(ch) || WHITELIST_SYMBOLS.has(ch)) {
            out += ch;
        } else {
            out += "_";
        }
    }
    out = out.replace(/_+/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
    if (out === "") {
        return "";
    }
    if (key_byte_length(out) > KEY_BYTE_BUDGET) {
        let trimmed = "";
        for (const ch of out) {
            if (key_byte_length(trimmed + ch) > KEY_BYTE_BUDGET) {
                break;
            }
            trimmed += ch;
        }
        out = trimmed.replace(/_+$/g, "");
    }
    return out;
}

/** 已成形 key 的安全校验（防御外部来源：变更流手输 id、历史配置）——
 *  与 normalize 幂等：safe key 必然等于自身归一化结果 */
export function is_safe_point_key(id: string): boolean {
    const s = String(id ?? "");
    return s !== "" && key_byte_length(s) <= MAX_POINT_KEY_BYTES &&
        normalize_point_name(s) === s;
}

/** 校验失败的用户可读原因（null = 合法） */
export function point_key_error(id: string, label: string): string | null {
    const s = String(id ?? "");
    if (s === "") {
        return `${label}为空`;
    }
    if (key_byte_length(s) > MAX_POINT_KEY_BYTES) {
        return `${label} "${s.slice(0, 32)}…" 太长（超过 ${MAX_POINT_KEY_BYTES} 字节），请保证在 1K 以内`;
    }
    if (s.includes(".")) {
        return `${label} "${s}" 含分隔符「.」——点 key 内不允许点号，请改用其他字符`;
    }
    if (normalize_point_name(s) !== s) {
        return `${label} "${s}" 含不允许的字符（空白/引号/逗号/等号/控制字符或未折叠全角），请修正点名`;
    }
    return null;
}
