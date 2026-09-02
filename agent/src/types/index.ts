// c4/agent/src/types/index.ts — 共享类型定义
// 根据 agent.md §3.2.1.1 / §3.2.1.2a / §3.1 / §3.3 定义

// ── Step Action ──────────────────────────────────────────
export type StepAction = "add" | "modify" | "delete";

// ── Service Point（判别联合）─────────────────────────────
// Writer 点用 `id` 标识（采集点名），Reader 点用 `key` 标识（引用 Writer 点），二者互斥——
// 用判别联合强制：id 与 key 恰好其一致合法，双缺或双填均被类型系统拒绝
export type ServicePoint = WriterPoint | ReaderPoint;

export interface WriterPoint {
    id: string;                   // Writer 点标识：采集点名（global key = {instance.id}.{point.id}）
    key?: never;                  // Writer 点无 key
    shm_id: number;               // 固定为 0，由 c4_shm_manager 分配后回填
    [field: string]: unknown;     // 业务字段由 point_schema.fields 声明（Writer / Reader 统一）
}

export interface ReaderPoint {
    id?: never;                   // Reader 点无 id
    key: string;                  // Reader 点标识：引用 Writer 点（值 = {writer_instance_id}.{point_id}）
    shm_id: number;               // 固定为 0，由 c4_shm_manager 分配后回填
    [field: string]: unknown;     // 业务字段由 point_schema.fields 声明（Writer / Reader 统一）
}

// ── Service Step ─────────────────────────────────────────
export interface ServiceStep {
    action: StepAction;
    service_type: string;
    instance: Record<string, unknown>;  // 含 id, name + 服务特有字段
    points: ServicePoint[];
}

// ── AccessPlan ───────────────────────────────────────────
// 实例 plan 字段直接平铺在 device/forward_target 上（不做语义分类），
// 由 registry 的 config_schema.fields 声明（无 default 键 = 必填）；点业务字段由 point_schema.fields 声明

export interface DevicePoint {
    name: string;                 // 点名称（对应 point.id；空字符串视为无点名，由身份字段生成）
    [field: string]: unknown;     // 协议特有字段由 point_schema.fields 声明（如 Modbus: addr/uid/fun/type/swap）
}

export interface DeviceSpec {
    name: string;                 // 设备名称（中文显示）
    abbr: string;                 // 采集目标标识（最终 id 以记忆库为准）
    protocol: string;             // 通信协议
    points: DevicePoint[];        // 采集点列表
    [field: string]: unknown;     // 实例 plan 字段直接平铺（ip/port 等，由 config_schema 声明）
}

export interface ForwardTargetSpec {
    name: string;                 // 目标名称（中文显示）
    abbr: string;                 // 转发目标标识（最终 id 以记忆库为准）
    protocol: string;             // 转发协议
    [field: string]: unknown;     // 实例 plan 字段 + 目标级字段（measurement 等，由 point_schema.fields 声明）
}

export interface AccessPlan {
    site: { name: string; abbr: string };
    devices: DeviceSpec[];
    forward_targets: ForwardTargetSpec[];
}

// ── Agent State ──────────────────────────────────────────
export type AgentPhase =
    | "idle"         // 空闲
    | "collecting"   // info-gatherer 收集
    | "planning"     // plan-generator 规划
    | "confirmed"    // 用户已确认
    | "executing";   // step-decomposer + 执行

export type AgentStatus = "success" | "error";

export interface AgentStateSummary {
    phase: AgentPhase;
    hasAccessPlan: boolean;
    lastError: string | null;
}

// ── Registry ─────────────────────────────────────────────
export interface RegistryProtocol {
    protocol: string;
    description: string;
    selection_rules: Array<{ condition: string; description: string }>;
}

export interface PointField {
    name: string;         // 字段名（如 addr/uid/fun/type/swap）
    type: string;         // 字段类型（如 "integer" / "string"）
    description?: string; // 字段描述（可缺省，L1 渲染时条件化）
}

/** 点表 schema（agent.md §3.3）：fields = 业务字段声明；identity_fields = 身份字段（Writer 必填，Reader 不填） */
export interface PointSchema {
    fields: PointField[];        // 点表业务字段声明（全部必须提供、无默认值）
    identity_fields?: string[];  // 能唯一标识一个点的字段子集（按字段名声明，顺序即生成点名拼接顺序）
}

export interface RegistryEntry {
    service_type: string;
    display_name: string;
    role: "writer" | "reader";
    protocols: RegistryProtocol[];
    point_schema: PointSchema;    // 点表 schema（fields + identity_fields）
    config_schema: {
        fields: Record<string, {
            type: string;
            default?: unknown;        // 缺省/null = 必填（用户必须提供）；有值 = 技术默认值，自动填充
            description?: string;
        }>;
    };
    binary_path: string;
    prompt_hints?: string[];      // 服务使用提示（可选，随 L1 注入系统提示）
    error_mappings: Record<string, string>;
}

export interface RegistryL1Summary {
    service_type: string;
    display_name: string;
    role: string;
    protocols: RegistryProtocol[];
}

// ── Agent Config ─────────────────────────────────────────
export interface AgentConfig {
    instance_id: string;
    model: {
        provider: string;
        name: string;
        temperature: number;
        max_tokens: number;
        api_key_env: string;
    };
    server: {
        host: string;
        port: number;
        cors_origin: string;
    };
    mcp_registry: { path: string };
    shm_manager: {
        binary: string;
        config_path: string;
    };
    state: { backend: string; path: string };
    logging: { level: string; dir: string; agent_level?: string };
    frontend?: { dir: string };
    site?: { name: string; abbr: string };
}

// ── Config Document ──────────────────────────────────────
export interface MCPInstanceConfig {
    id: string;
    name: string;
    [key: string]: unknown;
    points: ServicePoint[];
}

export interface ShmManagerSection {
    writer: string[];
    reader: string[];
}

export interface SystemConfig {
    c4_shm_manager: ShmManagerSection;
    [service_type: string]: MCPInstanceConfig[] | ShmManagerSection;
}
