// c4/agent/src/orchestrator/zh_numeral.ts — 自然语言起始地址提取（≤99999）
// 阶段6 转发地址判定（func_test_case 用例 5）：「转发地址使用从一万开始的地址」
// → 10000。定向匹配「从/自 X 开始/起」句式，不全文改写——避免把「一一对应」
// 这类普通词汇误转成数字（过度展开会隔断确定性范围正则的桥接）。

const ZH_DIGIT: Record<string, number> = {
    零: 0,
    一: 1,
    二: 2,
    两: 2,
    三: 3,
    四: 4,
    五: 5,
    六: 6,
    七: 7,
    八: 8,
    九: 9,
};

/** 万以下段（含十/百/千）求值；含非中文数字字符或无有效数字时返回 null。 */
function zh_under_wan(s: string): number | null {
    let total = 0;
    let num: number | null = null;
    let sawDigit = false;
    for (const ch of s) {
        if (ZH_DIGIT[ch] !== undefined) {
            num = ZH_DIGIT[ch];
            sawDigit = true;
        } else if (ch === "十") {
            total += (num ?? 1) * 10;
            num = null;
            sawDigit = true;
        } else if (ch === "百") {
            total += (num ?? 1) * 100;
            num = null;
            sawDigit = true;
        } else if (ch === "千") {
            total += (num ?? 1) * 1000;
            num = null;
            sawDigit = true;
        } else {
            return null;
        }
    }
    if (!sawDigit) return null;
    return total + (num ?? 0);
}

/** 单个中文数字串求值（支持万位组合）；无法解析返回 null。 */
function zh_convert(m: string): number | null {
    const wanIdx = m.indexOf("万");
    if (wanIdx < 0) return zh_under_wan(m);
    const head = zh_under_wan(m.slice(0, wanIdx));
    if (head === null) return null;
    const tail = m.slice(wanIdx + 1);
    if (tail === "") return head * 10000;
    const tailVal = zh_under_wan(tail);
    if (tailVal === null) return null;
    // 「一万二」短尾按千位补齐（12000）；「一万零九」按原位（10009）；「一万两千」= 12000
    if (tail.startsWith("零")) return head * 10000 + tailVal;
    return head * 10000 + (tailVal < 10 ? tailVal * 1000 : tailVal);
}

/**
 * 提取自然语言起始地址（「从/自/使用从 N 开始/起」，N 为阿拉伯或中文数字）。
 * 返回 null 表示消息中没有起始地址表述——调用方继续走其他提取/缺口路径。
 */
export function zh_start_address(text: string): number | null {
    const zhM = text.match(
        /(?:从|自|使用从)[\s，,]*([零一二两三四五六七八九十百千万]{1,10})(?:开始|起)/,
    );
    if (zhM) {
        const n = zh_convert(zhM[1]);
        if (n !== null) return n;
    }
    const arM = text.match(/(?:从|自|使用从)[\s，,]*(\d{2,7})(?:开始|起)/);
    if (arM) return Number(arM[1]);
    return null;
}
