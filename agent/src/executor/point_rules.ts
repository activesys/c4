// c4/agent/src/executor/point_rules.ts
// 点级不变式共享契约（agent.md §2.4.3，v0.5.0）——规则只写一次，四层引用：
//   output_device_info / output_access_plan / output_plan_steps / merge_config_from_steps。
// 比较域 = merge 后该 (service_type, instance) 的最终点表（批次内 + 批次与既有点）。
//
// 类型枚举对齐 c4/mcp/internal/protocol/const.go（Boolean=0 … LargeDataBlock=16）。
// 跨度单位 = Modbus 寄存器（16bit）。
//
// 层级适用范围（agent.md §2.4.3）：
//   - output_access_plan 层：仅对「身份+type 字段齐全」的点执行 overlap/duplicate，
//     字段不齐交由 plan_steps 层拦截（本模块对身份不全的点跳过判定，天然满足）。
//   - fun ∈ {1, 2}（线圈/离散输入）为位编址，不适用寄存器跨度重叠，由身份查重覆盖。

/** 可参与判定的最小点形状——多余字段透传忽略。 */
export interface PointLike {
    addr?: number;
    uid?: number;
    fun?: number;
    type?: number;
    name?: string;
    id?: string;
}

/** 17 型寄存器跨度全表；变长类型（12/13/14/16）不出现在表中 = fail-closed。 */
export const REGISTER_SPAN: Readonly<Record<number, number>> = {
    0: 1,    // Boolean（位编址场景见 BIT_FUNS）
    1: 1,    // Int8
    2: 1,    // Uint8
    3: 1,    // Int16
    4: 1,    // Uint16
    5: 2,    // Int32
    6: 2,    // Uint32
    7: 4,    // Int64
    8: 4,    // Uint64
    9: 1,    // Float16（IEC 60870-5-104 NVA，16bit）
    10: 2,   // Float32
    11: 4,   // Float64
    15: 1,   // Bit
};

/** 位编址功能码：线圈(1)/离散输入(2)——不适用寄存器跨度重叠。 */
export const BIT_FUNS: ReadonlySet<number> = new Set([1, 2]);

/** 寄存器跨度；变长/未知类型返回 null（fail-closed，调用方拒绝并给出可读错误）。 */
export function register_span(type: number | undefined): number | null {
    if (typeof type !== "number" || !Number.isInteger(type) || type < 0) {
        return null;
    }
    return REGISTER_SPAN[type] ?? null;
}

/** 变长/未知类型的可读错误（fail-closed 用）。 */
export function span_error(type: number | undefined, label: string): string {
    return `${label} 的数据类型 ${String(type)} 为变长或未知类型，无法静态判定寄存器跨度，`
        + `请改用定长类型（int16/uint16/int32/uint32/float32/int64/uint64/float64 等）`;
}

/** 点身份键；身份字段（uid/fun/addr）不全时返回 null——跳过判定（层级适用范围见文件头）。 */
export function point_identity(p: PointLike): string | null {
    if (typeof p.uid !== "number" || typeof p.fun !== "number" || typeof p.addr !== "number") {
        return null;
    }
    return `uid=${p.uid}, fun=${p.fun}, addr=${p.addr}`;
}

function point_label(p: PointLike, index: number): string {
    return p.name ? `「${p.name}」(第 ${index + 1} 个点)` : `第 ${index + 1} 个点`;
}

/**
 * 身份组合重复检测（uid+fun+addr）。
 * 身份不全的点跳过（由 check_required_fields 拦截）。
 * 返回可读错误数组；空数组 = 通过。
 */
export function check_duplicate_points(points: PointLike[]): string[] {
    const errors: string[] = [];
    const seen = new Map<string, number[]>();
    for (let i = 0; i < points.length; i++) {
        const key = point_identity(points[i]);
        if (key === null) {
            continue;
        }
        const list = seen.get(key) ?? [];
        list.push(i);
        seen.set(key, list);
    }
    for (const [key, idx] of seen) {
        if (idx.length > 1) {
            const names = idx.map((i) => point_label(points[i], i)).join("、");
            errors.push(`点重复：身份组合（${key}）出现 ${idx.length} 次——${names}。`
                + `同一身份无法作为两个不同的点，这是点表的问题，未写入任何变更`);
        }
    }
    return errors;
}

/**
 * 寄存器区间重叠检测：同 (uid, fun) 组内 [addr, addr+span) 相交即违例。
 * - fun ∈ BIT_FUNS（位编址）跳过跨度判定，由身份查重覆盖；
 * - 身份不全或跨度 fail-closed（变长/未知类型）的点：变长类型直接报可读错误（fail-closed），
 *   身份不全跳过；
 * - 比较域由调用方保证：传入 merge 后的最终点表（批次内 + 批次与既有点）。
 */
export function check_shm_overlap(points: PointLike[]): string[] {
    const errors: string[] = [];
    interface Entry { addr: number; span: number; index: number; }
    const groups = new Map<string, Entry[]>();
    for (let i = 0; i < points.length; i++) {
        const p = points[i];
        const key = point_identity(p);
        if (key === null) {
            continue;
        }
        if (BIT_FUNS.has(p.fun as number)) {
            continue;
        }
        const span = register_span(p.type);
        if (span === null) {
            errors.push(span_error(p.type, point_label(p, i)));
            continue;
        }
        const gkey = `uid=${p.uid}, fun=${p.fun}`;
        const list = groups.get(gkey) ?? [];
        list.push({ addr: p.addr as number, span, index: i });
        groups.set(gkey, list);
    }
    for (const [gkey, list] of groups) {
        const sorted = [...list].sort((a, b) => a.addr - b.addr);
        for (let i = 1; i < sorted.length; i++) {
            const prev = sorted[i - 1];
            const cur = sorted[i];
            if (cur.addr < prev.addr + prev.span) {
                errors.push(
                    `点重叠：${point_label(points[cur.index], cur.index)} addr=${cur.addr} 与 `
                    + `${point_label(points[prev.index], prev.index)} addr=${prev.addr}`
                    + `（跨度 ${prev.span} 个寄存器）在组 (${gkey}) 上寄存器区间重叠，`
                    + `未写入任何变更——请调整地址使各点区间 [addr, addr+跨度) 互不相交`);
            }
        }
    }
    return errors;
}

/**
 * 逐点必填字段检测（point_schema.fields）。
 * 返回可读错误数组，逐点列出缺失字段（风格对齐「缺少必要字段 uid; 缺少必要字段 fun」）。
 */
export function check_required_fields(
    points: PointLike[],
    required: readonly string[],
    label = "设备",
): string[] {
    const errors: string[] = [];
    for (let i = 0; i < points.length; i++) {
        const p = points[i] as Record<string, unknown>;
        const missing = required.filter(
            (f) => p[f] === undefined || p[f] === null || p[f] === "",
        );
        if (missing.length > 0) {
            errors.push(
                `${label} 的第 ${i + 1} 个点`
                + missing.map((f) => `缺少必要字段 "${f}"`).join("; "),
            );
        }
    }
    return errors;
}

/**
 * 功能码合法性（Modbus）：合法读取功能码 1(线圈)/2(离散输入)/3(保持寄存器)/4(输入寄存器)。
 * fun 字段缺失跳过（非 modbus 点，如 asfp2/influxdb）；越界值 → 可读错误（fail-closed）。
 * 2026-09-19 实测：「全是功能码9」一路通过——本规则由此补入。
 */
export const MODBUS_LEGAL_FUNS: ReadonlySet<number> = new Set([1, 2, 3, 4]);

export function check_fun_codes(points: PointLike[]): string[] {
    const errors: string[] = [];
    for (let i = 0; i < points.length; i++) {
        const f = points[i].fun;
        if (typeof f !== "number") {
            continue;
        }
        if (!MODBUS_LEGAL_FUNS.has(f)) {
            errors.push(
                `第 ${i + 1} 个点的功能码 ${f} 非法——Modbus 合法读取功能码：`
                + `1(线圈) / 2(离散输入) / 3(保持寄存器) / 4(输入寄存器)。请向用户澄清`,
            );
        }
    }
    return errors;
}

/**
 * 从站号来源校验（agent.md「必填项用户提供原则」）：
 * declaredUids = 从用户消息确定性捕获的从站号集合（super_worker 捕获，同 userPort 机制）。
 * - 集合为空且点带 uid → uid 必为 LLM 编造 → 可读错误要求向用户询问；
 * - 集合非空且点的 uid ∉ 集合 → 与用户声明不符 → 可读错误要求澄清；
 * - uid 字段缺失跳过（由必填字段校验拦截）。
 */
export function check_uid_provenance(
    points: PointLike[],
    declared: ReadonlySet<number>,
): string[] {
    const errors: string[] = [];
    if (declared.size === 0) {
        if (points.some((p) => typeof p.uid === "number")) {
            errors.push(
                "从站号（uid）未由用户提供——禁止编造或使用默认值。"
                + "请向用户询问每个采集点的从站号（单元标识符）",
            );
        }
        return errors;
    }
    const declaredList = [...declared].join("、");
    for (let i = 0; i < points.length; i++) {
        const u = points[i].uid;
        if (typeof u !== "number") {
            continue;
        }
        if (!declared.has(u)) {
            errors.push(
                `第 ${i + 1} 个点的从站号 uid=${u} 与用户提供的从站号（${declaredList}）不符，`
                + `请向用户澄清，禁止自行改用其他值`,
            );
        }
    }
    return errors;
}

/**
 * 一站式最终点表校验（merge 前置/plan_steps 用）：
 * 必填字段（可选）→ 身份查重 → 功能码合法 → 区间重叠。
 */
export function validate_point_table(
    points: PointLike[],
    opts?: { required?: readonly string[]; label?: string },
): string[] {
    const errors: string[] = [];
    if (opts?.required) {
        errors.push(...check_required_fields(points, opts.required, opts?.label));
    }
    errors.push(...check_duplicate_points(points));
    errors.push(...check_fun_codes(points));
    errors.push(...check_shm_overlap(points));
    return errors;
}
