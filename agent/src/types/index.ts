// c4/agent/src/types/index.ts — 共享类型定义
// 根据 agent.md §3.2.1 定义

// ── Step Action ──────────────────────────────────────────
export type StepAction = "add" | "modify" | "delete";

// ── Service Point ────────────────────────────────────────
export interface ServicePoint {
  id: string;                   // 采集点标识符
  addr: number;                 // 协议地址
  uid?: number;                 // Modbus: 单元标识符
  fun?: number;                 // Modbus: 功能码
  type?: number;                // Modbus: 数据类型枚举
  swap?: number;                // Modbus: 字节交换大小
  key?: string;                 // Reader: 引用的 Writer key
  shm_id: number;               // 全局 shm_id，默认 0
}

// ── Service Step ─────────────────────────────────────────
export interface ServiceStep {
  action: StepAction;
  service_type: string;
  instance: Record<string, unknown>;  // 含 id, name + 服务特有字段
  points: ServicePoint[];
}

// ── AccessPlan ───────────────────────────────────────────
export interface DevicePoint {
  name: string;
  addr: number;
  uid?: number;
  fun?: number;
  type?: number;
  swap?: number;
}

export interface DeviceSpec {
  name: string;
  seq: number;
  protocol: string;
  connection: { ip: string; port: number };
  points: DevicePoint[];
}

export interface ForwardTargetSpec {
  name: string;
  protocol: string;
  connection: { ip: string; port: number };
  point_addr_start?: number;
}

export interface AccessPlan {
  site: { name: string; abbr: string };
  devices: DeviceSpec[];
  forward_targets: ForwardTargetSpec[];
}

// ── Agent State ──────────────────────────────────────────
export type AgentPhase =
  | "idle"
  | "parsing"
  | "parsed"
  | "plan_ready"
  | "confirmed"
  | "configuring"
  | "starting";

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

export interface RegistryEntry {
  service_type: string;
  display_name: string;
  role: "writer" | "reader";
  protocols: RegistryProtocol[];
  config_schema: {
    required: string[];
    fields: Record<string, {
      type: string;
      source: "plan" | "default";
      default: unknown;
      description: string;
    }>;
  };
  binary_path: string;
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
    instance_id: string;
    config_path: string;
  };
  state: { backend: string; path: string };
  logging: { level: string; dir: string };
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
