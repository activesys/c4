// c4/agent/src/orchestrator/multi_target.ts — 多设备/多下游确定性声明解析
// agent.md §2.12（多下游接入，协议无关）：首接一次声明的确定性解析（P1 入口 A）与
// 点集表达式（P2 入口 C）。llm 提取层不感知多目标——本模块在提取层之前对
// 「结构完整的声明」做确定性拆解，解析成功时由编排层跳过对应单目标提取相。
// 解析失败/形态不符 → 返回 null，回落既有单目标路径（不猜测，§2.12.3）。

// 设备类型词（与 device_prefix_candidate 的类型映射对齐，用于设备名识别）
const DEV_TYPE_RE =
    /(?:风机|水泵|控制器|升压站|逆变器|测风塔|主变|光伏|储能|电机|变压器)/;
// 设备名：可选编号 + 类型词（如 1号风机、升压站、2号风机B）
const DEV_NAME_RE = new RegExp(`[\\u4e00-\\u9fa5A-Za-z0-9]{1,8}?${DEV_TYPE_RE.source}`, "g");

export interface DeviceDecl {
    name: string;
    protocol: string | null; // canonical 猜测（asfp2/influxdb/modbus/iec104），未声明为 null
    points: Array<{ addr: number; name: string }>;
    port: number | null;
    text: string;
}

export interface TargetDecl {
    name: string | null;
    protocol: string | null; // asfp2/influxdb/…；未声明为 null
    conn: Record<string, unknown>; // ip/port/url/token/org/bucket/measurement
    points: Array<Record<string, unknown>>; // addr 形态（asfp2）或 {measurement?,field?,type?} 骨架（influx）
    pointsMode: "addrs" | "mirrorAll" | "expr" | null;
    exprText: string | null; // 点集表达式原文（pointsMode=expr）
    mirrorDevice: string | null; // pointsMode=mirrorAll 时引用的设备名
    fieldFromSource: boolean; // 「字段名跟点名对应」→ field 由源点 id 派生
    deviceRefs: string[];
    raw: string;
}

/** 规范化协议词（与 alias 层的 canonical 对齐的确定性子集）。 */
function canon_protocol(word: string): string | null {
    const w = word.toLowerCase();
    if (/asfp ?2|asfp/.test(w)) return "asfp2";
    if (/influx|时序|入库/.test(w)) return "influxdb";
    if (/modbus/.test(w)) return "modbus";
    if (/iec ?104|104规约/.test(w)) return "iec104";
    return null;
}

/** 「1000:风速、1001:功率」/「1000 风速 1001 功率」形态的 addr:name 对。 */
export function parse_addr_name_pairs(text: string): Array<{ addr: number; name: string }> {
    const out: Array<{ addr: number; name: string }> = [];
    // 点名不含数字/冒号——「1010:机舱振动和1011:机舱温度」的贪婪点名会把下一个
    // 地址连同冒号吞进名字（用例 66② 实测只解析出 1 对）；尾部连接词/量词剥除
    const re = /(\d{1,6})\s*[:：]\s*([^\d\s，。；,、:：]+)/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
        // 名字在连接词/量词处截断（「机舱温度两点也转给…」→「机舱温度」）
        const name = m[2]
            .split(/(?:两点|一点|三点|个点|也|再|还|就|然后)/)[0]
            .replace(/[和与]+$/, "");
        out.push({ addr: Number(m[1]), name });
    }
    return out;
}

/**
 * 多设备声明拆分（agent.md §2.12.2 入口 A，≤2 台逐台口径的结构性解析）。
 * 识别「N号风机…个点从A到B…使用端口P」重复形态；解析成功（≥2 台且各自带
 * 点表与端口）返回设备声明列表，否则 null（回落单设备提取，不猜测）。
 */
export function split_device_decls(semantic: string): DeviceDecl[] | null {
    if ((semantic.match(/个点从/g) ?? []).length < 2) return null;
    const names = [...semantic.matchAll(DEV_NAME_RE)].map((m) => m[0]);
    const uniqNames = [...new Set(names)].filter((n) =>
        new RegExp(`${n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\d+\\s*个点从`).test(semantic),
    );
    if (uniqNames.length < 2 || uniqNames.length > 2) return null; // 逐台口径 ≤2 台
    // 段边界锚定声明位置（设备名后紧跟「N个点从」）而非任意出现位置——
    // 「接入1号风机和2号风机的数据」首段里 1号风机 后紧跟 2号风机，
    // 按任意出现位置切会把 1 号机切成「1号风机和」（点表解析必然失败）
    const declIdx = new Map<string, number>();
    for (const n of uniqNames) {
        const dm = semantic.match(
            new RegExp(`${n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\d+\\s*个点从`),
        );
        if (!dm || dm.index === undefined) return null;
        declIdx.set(n, dm.index);
    }
    // 共享协议：「都是X协议」/「均采用X」
    let shared: string | null = null;
    const sm = semantic.match(/(?:都是|均为?|都采用|均采用)\s*([a-zA-Z0-9]+)\s*协议/);
    if (sm) shared = canon_protocol(sm[1]);
    const decls: DeviceDecl[] = [];
    for (const name of uniqNames) {
        const idx = declIdx.get(name) ?? -1;
        const segStart = idx;
        const segEnds = uniqNames
            .map((n) => declIdx.get(n) ?? -1)
            .filter((x) => x > idx);
        const segEnd = segEnds.length > 0 ? Math.min(...segEnds) : semantic.length;
        const seg = semantic.slice(segStart, segEnd);
        const rangeM = seg.match(/(\d+)\s*个点从\s*(\d+)\s*到\s*(\d+)/);
        const portM = seg.match(/使用端口\s*(\d{1,5})/);
        if (!rangeM) return null;
        const begin = Number(rangeM[2]);
        const end = Number(rangeM[3]);
        const count = Number(rangeM[1]);
        if (end - begin + 1 !== count) return null;
        let points = parse_addr_name_pairs(seg).filter((p) => p.addr >= begin && p.addr <= end);
        if (points.length === 0) {
            // 「点名跟1号风机一样对应1100~1109」镜像形态：按声明序镜像首设备点名
            const mirrorM = seg.match(/点名跟(?:[\u4e00-\u9fa5A-Za-z0-9]+)?(?:一样|相同)对应\s*(\d+)\s*[~～到]\s*(\d+)/);
            const first = decls[0];
            if (mirrorM && first && first.points.length > 0) {
                const b = Number(mirrorM[1]);
                const e = Number(mirrorM[2]);
                if (e - b + 1 !== first.points.length) return null;
                points = first.points.map((p, i) => ({ addr: b + i, name: p.name }));
            } else {
                return null;
            }
        }
        if (points.length !== count) return null;
        decls.push({
            name,
            protocol: shared ?? canon_protocol(seg.match(/([a-zA-Z0-9]+)\s*协议/)?.[1] ?? "") ,
            points,
            port: portM ? Number(portM[1]) : null,
            text: seg,
        });
    }
    return decls;
}

/**
 * 多下游目标声明拆分（agent.md §2.12.2 入口 A）：在消息中识别 ≥2 个
 * 「(转发|写入|发送|上传)到 X」子句并逐句解析结构。仅 1 个目标 → null
 * （回落既有单目标提取路径，零回归）。
 */
export function split_target_decls(semantic: string): TargetDecl[] | null {
    const clauses = semantic.split(/[；;\n]/).map((c) => c.trim()).filter((c) => c !== "");
    const targets: TargetDecl[] = [];
    for (const clause of clauses) {
        const nm = clause.match(/(?:数据|数据点)?(?:也)?(?:转发到|写入到|发送到|上传到|写入|转发给)\s*([^\s，。,：:]{2,20})/);
        if (!nm) continue;
        const tname = nm[1].replace(/^(?:到|给)/, "");
        // 「X的数据(也)转发到 Y」/「把X的数据转发给 Y」→ deviceRefs
        const refs: string[] = [];
        const refHead = clause.match(/([\u4e00-\u9fa5A-Za-z0-9]{2,14}?)的数据/);
        if (refHead && DEV_TYPE_RE.test(refHead[1])) refs.push(refHead[1]);
        const refAll = clause.match(/(?:把)?([\u4e00-\u9fa5A-Za-z0-9]{1,8}(?:号)?(?:风机|升压站|逆变器|测风塔|主变|光伏|储能))的数据/);
        if (refAll && !refs.includes(refAll[1])) refs.push(refAll[1]);
        // 协议：转发采用X协议 / 写入→influxdb
        let proto: string | null = null;
        const pm = clause.match(/(?:转发|写入|发送|上传)采用\s*([a-zA-Z0-9]+)\s*协议/);
        if (pm) proto = canon_protocol(pm[1]);
        const conn: Record<string, unknown> = {};
        if (proto === null && (/写入|入库/.test(clause) || /https?:\/\//.test(clause))) {
            proto = "influxdb";
        }
        // conn：ip:port / url/token/org/bucket
        const addrM = clause.match(/(?:目标地址(?:是)?|地址)\s*(?:是)?\s*(\d{1,3}(?:\.\d{1,3}){3})\s*[:：]\s*(\d{1,5})/);
        if (addrM) {
            conn["ip"] = addrM[1];
            conn["port"] = Number(addrM[2]);
        }
        const urlM = clause.match(/(https?:\/\/[^\s，。,]+)/);
        if (urlM) conn["url"] = urlM[1];
        const tokenM = clause.match(/token(?:是|[:：=])\s*([^\s，。,]+)/i);
        if (tokenM) conn["token"] = tokenM[1];
        const orgM = clause.match(/org(?:是|[:：=])\s*([^\s，。,]+)/i);
        if (orgM) conn["org"] = orgM[1];
        const bucketM = clause.match(/bucket(?:是|换成|[:：=])\s*([^\s，。,]+)/i);
        if (bucketM) conn["bucket"] = bucketM[1];
        const measM = clause.match(/(?:全部写进|写进|表名为?)\s*([^\s，。,]+)/);
        if (measM && measM[1] !== "") conn["measurement"] = measM[1];
        const fieldFromSource = /字段名跟?点名对应|字段名跟.{0,8}一样/.test(clause);
        // 点表：点表A~B / 点表同样A~B / 「N个点全部写进M」→ 镜像
        const points: Array<Record<string, unknown>> = [];
        let mode: TargetDecl["pointsMode"] = null;
        let exprText: string | null = null;
        let mirrorDevice: string | null = null;
        const ptM = clause.match(/点表(?:同样|也)?\s*(\d+)\s*[~～]\s*(\d+)/);
        const allM = clause.match(
            /(?:这\s*\d+\s*个点|\d+\s*个点全部(?:写进|写入)|全部(?:的)?点|这批数据|全部数据)/,
        );
        if (ptM) {
            const b = Number(ptM[1]);
            const e = Number(ptM[2]);
            for (let a = b; a <= e; a++) points.push({ addr: a });
            mode = "addrs";
        } else if (allM && proto === "influxdb") {
            mode = "mirrorAll";
            const devM = clause.match(/([\u4e00-\u9fa5A-Za-z0-9]{1,8}(?:号)?(?:风机|升压站|逆变器|测风塔|主变|光伏|储能))/);
            mirrorDevice = devM ? devM[1] : refs[0] ?? null;
        } else {
            // 点集表达式（「：」后到连接参数前的片段）；URL 中的「://」不是表达式
            // 起点（func_test_case 63「写入地址http://…」实测）——含 url 痕迹即放弃
            const ex = clause.match(/[:：]\s*([^\n]+?)(?:，?写入地址|，?目标地址|$)/);
            if (
                ex &&
                /\d|点名|前|后|全部/.test(ex[1]) &&
                !/https?:\/\/|^\/\//.test(ex[1])
            ) {
                exprText = ex[1];
                mode = "expr";
            }
        }
        targets.push({
            name: tname,
            protocol: proto,
            conn,
            points,
            pointsMode: mode,
            exprText,
            mirrorDevice,
            fieldFromSource,
            deviceRefs: refs,
            raw: clause,
        });
    }
    return targets.length >= 2 ? targets : null;
}

/** 同参判定（agent.md §2.12.5 第 3 行）：协议 + 连接必要项全同 + 点集相同。 */
export function same_target_params(
    a: { protocol: string | null; conn: Record<string, unknown>; pointSig: string },
    b: { protocol: string | null; conn: Record<string, unknown>; pointSig: string },
): boolean {
    if (a.protocol !== b.protocol) return false;
    const keys = new Set([...Object.keys(a.conn), ...Object.keys(b.conn)]);
    for (const k of keys) {
        if (k === "measurement" || k === "token") continue; // 展示/凭据差异不构成异参
        if (String(a.conn[k] ?? "") !== String(b.conn[k] ?? "")) return false;
    }
    return a.pointSig === b.pointSig;
}

/** 点集签名（同参比较用，跨 addr 形态与 influx 形态统一为序        列化键）。 */
export function point_sig(points: Array<Record<string, unknown>>): string {
    return points
        .map((p) =>
            p["addr"] !== undefined
                ? `a${Number(p["addr"])}`
                : `${String(p["measurement"] ?? "")}.${String(p["field"] ?? "")}`,
        )
        .join("|");
}

export interface ExprDevice {
    name: string;
    points: Array<{ addr: number; name: string; id: string }>; // 既有点（含 key 生效 id）
}

export interface ExprResult {
    points: Array<{ device: string; addr: number; name: string; id: string }>;
    error: string | null;
}

/**
 * 点集表达式解析（agent.md §2.12.3，确定性文法）：
 *   点集表达式 := 段 (（和|与|、|，) 段)*        —— 段间并集
 *   段 := 设备序列 选择器*                      —— 无选择器 = 全部；多选择器空格并列并集
 *   设备序列 := 设备名 (（和|与） 设备名)*      —— 贪婪吸收至选择器出现
 *   选择器 := 全部 | 前N个 | 后N个 | 地址范围 | 点名列表（/或空格分隔）
 * 「前/后N个」按设备点表声明顺序；设备名以注册全名精确命中（不猜测）。
 */
export function parse_point_set_expr(expr: string, devices: ExprDevice[]): ExprResult {
    const out: Array<{ device: string; addr: number; name: string; id: string }> = [];
    const globalSeen = new Set<string>(); // 跨段去重（净集语义，§2.12.3）
    const segments = expr.split(/[、，]/).map((s) => s.trim()).filter((s) => s !== "");
    for (const seg of segments) {
        // 设备序列：贪婪吸收「和/与」连接的注册设备名，直到选择器出现
        const devNames: string[] = [];
        let rest = seg;
        for (;;) {
            const hit = devices.find((d) => rest.startsWith(d.name));
            if (!hit || devNames.includes(hit.name)) break;
            devNames.push(hit.name);
            // 「的」是设备名与选择器间的口语连接词（「1号风机的前5个点」），剥除
            rest = rest.slice(hit.name.length).replace(/^(?:和|与|的)/, "").trim();
        }
        if (devNames.length === 0) {
            return { points: out, error: `无法识别表达式「${seg}」中的设备名（请使用注册设备全名）` };
        }
        // 选择器解析（空格并列多选择器，并集）
        const selText = rest.trim();
        const selectors: Array<{ kind: string; n?: number; b?: number; e?: number; names?: string[] }> = [];
        if (selText === "" || /^全部$/.test(selText)) {
            selectors.push({ kind: "all" });
        } else {
            const tokens = selText.split(/\s+/).filter((t) => t !== "");
            for (const tk of tokens) {
                const qm = tk.match(/^前(\d+)个点?$/);
                const hm = tk.match(/^后(\d+)个点?$/);
                const rm = tk.match(/^(\d+)\s*[~～]\s*(\d+)$/);
                if (qm) selectors.push({ kind: "first", n: Number(qm[1]) });
                else if (hm) selectors.push({ kind: "last", n: Number(hm[1]) });
                else if (rm) selectors.push({ kind: "range", b: Number(rm[1]), e: Number(rm[2]) });
                else {
                    // 点名列表（/ 或单 token 内）——无法识别的词整段澄清
                    const names = tk.split("/").map((x) => x.trim()).filter((x) => x !== "");
                    if (names.length === 0 || names.some((x) => !x)) {
                        return { points: out, error: `无法识别选择器「${tk}」` };
                    }
                    selectors.push({ kind: "names", names });
                }
            }
        }
        for (const dn of devNames) {
            const dev = devices.find((d) => d.name === dn);
            if (!dev) {
                return { points: out, error: `设备「${dn}」不存在或未接入` };
            }
            const pts = dev.points;
            const picked: typeof pts = [];
            for (const sel of selectors) {
                if (sel.kind === "all") picked.push(...pts);
                else if (sel.kind === "first") picked.push(...pts.slice(0, sel.n));
                else if (sel.kind === "last") picked.push(...pts.slice(Math.max(0, pts.length - (sel.n ?? 0))));
                else if (sel.kind === "range") {
                    picked.push(...pts.filter((p) => p.addr >= (sel.b ?? 0) && p.addr <= (sel.e ?? 0)));
                } else if (sel.kind === "names") {
                    for (const nm of sel.names ?? []) {
                        const hit = pts.find((p) => p.name === nm);
                        if (!hit) {
                            return { points: out, error: `设备「${dn}」没有点名「${nm}」的点` };
                        }
                        picked.push(hit);
                    }
                }
            }
            // 同段多选择器并集去重 + 跨段净集去重（同设备内按 addr 去重）
            const seen = new Set<number>();
            for (const p of picked) {
                if (seen.has(p.addr)) continue;
                const gk = `${dn}:${p.addr}`;
                if (globalSeen.has(gk)) continue;
                seen.add(p.addr);
                globalSeen.add(gk);
                out.push({ device: dn, addr: p.addr, name: p.name, id: p.id });
            }
        }
    }
    if (out.length === 0) return { points: out, error: "点集表达式解析结果为空" };
    return { points: out, error: null };
}
