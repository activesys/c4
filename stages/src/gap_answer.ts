// c4/agent/src/orchestrator/gap_answer.ts — 缺口应答词形解析纯函数层
// 2026-10-17 生产野测（景观SCADA 168 点缺 uid 接入收摊）：字段缺口的追问文案
// 教用户的词是「uid」（「缺少必要字段 "uid"」），绑定器却只认「从站」前缀词形
// ——「uid是1」「uid:1」裸「1」三连落空，gapRepeat 计满强制收摊。
// 词形对齐原则：绑定器必须认识问题文案里出现过的每一个词 + 常见口语变体
// （提问即语境锁定——缺口应答语境下裸数字即所指字段的值）。

// 词形绑定：「从站号都是1」（2026-10-02 链步31）/「从站号:1」/「uid是1」/
// 「uid:1」/「UID 都是 1」（词与数字间允许 ≤4 个非数字字符：是/为/都是/均为/:/=/空格）
const UID_WORD_RE = /(?:从站号?|uid)[^0-9]{0,4}(\d{1,3})/i;
// 裸值兜底：「1」「都是1」「全是1」「均为1」——仅限缺口应答语境调用（提问
// 已锁定语义），纯数字 1~3 位（uid 取值域 0~247，4 位以上不构成合法应答）
const UID_BARE_RE = /^\s*(?:(?:都|全|均)(?:是|为)?\s*)?(\d{1,3})\s*$/;

/**
 * 从站号（uid）缺口应答解析：返回应答的 uid 值；无法解析返回 null。
 */
export function uid_answer_of(text: string): number | null {
    const m = UID_WORD_RE.exec(text) ?? UID_BARE_RE.exec(text);
    if (m === null) return null;
    const v = Number(m[1]);
    return Number.isFinite(v) ? v : null;
}

/**
 * 转发侧文件通道门控（2026-10-18 生产野测裁定）：采集侧文件不得自动充作转发
 * 点表——生产实例：用户应「接入点表」之问上传景观SCADA 文件，转发提取的
 * <file_data> 例外条款（提示词级软约束）把文件 modbus 地址列（0 起步）当转发
 * 地址产出 69 点，连带 99 个采集点「无下游引用不接入」被排除。
 * 确定性门控，仅两种语境放行：
 *   ① 上传时刻系统正在问转发点表缺口（pendingGap === "fwd.points"，提问即
 *      语境锁定——本通道的设计场景，2026-09-29 用例11 缺陷C）；
 *   ② 用户本轮消息明确说了转发点表/转发地址（显式指认，不猜同义变体——
 *      弃权即继续追问，代价一次追问而非静默错绑）。
 */
export function fwd_file_gate(
    pendingGap: string | null,
    semantic: string,
): boolean {
    if (pendingGap === "fwd.points") return true;
    return /转发点表|转发地址/.test(semantic);
}

// ── addr 缺口应答绑定（2026-10-27 生产实测：点击量 10 点缺 addr，四轮应答
//    「接入地址从1000开始」「电量1的地址是1000依次类推」「电量1：1000，电量2：1001」
//    全部落空 gapRepeat 计满收摊——uid 有绑定通道而 addr 没有，词形对齐原则同样适用）

/** 点名token：中文/字母/数字/常见符号（不含冒号、逗号、分号——分隔符） */
const POINT_NAME = "[^：:，,；;\\s]{1,32}";
/** 全/半角冒号分隔的「点名:地址」对 */
const PAIR_RE = new RegExp(`(${POINT_NAME})\\s*[：:]\\s*(\\d{1,6})`, "g");

/**
 * 逐点地址映射解析：「电量1：1000，电量2：1001」「3000:风速」反向同样认
 * （「1000:电量1」）——全/半角冒号皆可。返回按出现序的（点名, 地址）对；
 * 无命中返回 null。
 */
export function addr_point_map_of(
    text: string,
): Array<{ name: string; addr: number }> | null {
    const out: Array<{ name: string; addr: number }> = [];
    for (const m of text.matchAll(PAIR_RE)) {
        out.push({ name: m[1]!.trim(), addr: Number(m[2]) });
    }
    return out.length > 0 ? out : null;
}

/**
 * 起始地址（+可选步长）解析：「接入地址从1000开始」「地址从10000起」「起始
 * 地址 1000，步长2」——返回 { start, step? }；无法解析返回 null。排除转发
 * 语境词（转发/目标——该应答锁定接入侧缺口）。
 */
export function addr_start_of(
    text: string,
): { start: number; step?: number } | null {
    if (/转发|目标/.test(text)) return null;
    let m = text.match(/(?:接入)?地址[^0-9]{0,6}从\s*(\d{1,6})\s*(?:开始|起)/);
    if (m === null) m = text.match(/(?:起始|开始)?地址[^0-9]{0,6}(\d{1,6})/);
    if (m === null) return null;
    const start = Number(m[1]);
    if (!Number.isFinite(start)) return null;
    const sm = text.match(/步长[^0-9]{0,4}(\d{1,4})/);
    return { start, ...(sm ? { step: Number(sm[1]) } : {}) };
}

/**
 * 首点锚定解析：「电量1的地址是1000，依次类推」「电量1是1000，其余顺延」——
 * 返回 { name, addr }；无法解析返回 null。
 */
export function addr_anchor_of(text: string): { name: string; addr: number } | null {
    const m = text.match(
        new RegExp(`(${POINT_NAME})[^0-9]{0,8}地址?[^0-9]{0,4}(\\d{1,6})`),
    );
    if (m === null) return null;
    if (!/依次|类推|顺延|其余|往后|以此/.test(text)) return null;
    // 剥尾部助词「的」（「电量1的地址是1000」——贪婪匹配会把「的」吞进名字，
    // 导致与点名「电量1」精确匹配失败）
    const name = m[1]!.trim().replace(/的$/, "");
    return { name, addr: Number(m[2]) };
}
