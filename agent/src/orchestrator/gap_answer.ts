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
