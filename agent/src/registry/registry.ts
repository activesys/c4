// c4/agent/src/registry/registry.ts — McpServiceRegistry 单例
// 根据 agent.md §3.3 实现
// 双层注入：L1 = 服务摘要（注入 SuperWorker 系统提示）
//           L2 = 完整定义（step-decomposer 按需查询）

import { loadRegistryFiles, type RegistryEntryValidated } from "./loader.js";
import type {
  PointField,
  RegistryEntry,
  RegistryL1Summary,
} from "./types.js";

// ── L1 摘要扩展类型（agent.md §3.3.0）──
// RegistryL1Summary 只含基础字段（service_type/display_name/role/protocols），
// 此处扩展 point_schema 与 config_schema 字段摘要。

/** config_schema 字段的 L1 摘要（供 info-gatherer 判断必填/可选）。 */
export interface L1PlanFieldSummary {
    name: string;
    type: string;
    /** 无 default 键（或 null）→ 必填；有默认值 → 可选 */
    required: boolean;
    /** 默认值（必填时为 undefined） */
    default: unknown;
    description?: string;
}

/** L1 摘要中的 point_schema（fields + Writer 身份字段）。 */
export interface L1PointSchema {
    fields: PointField[];
    identity_fields?: string[];
}

/** 完整 L1 服务摘要 = 基础摘要 + point_schema + config_schema 字段摘要 + 服务使用提示。 */
export interface ServiceCatalogEntry extends RegistryL1Summary {
    point_schema: L1PointSchema;
    plan_fields: L1PlanFieldSummary[];
    prompt_hints: string[];
}

// ── 内置错误翻译（agent.md §3.4）──
const BUILTIN_ERROR_TRANSLATIONS: Record<string, string> = {
  SHM_CORRUPTED: "数据存储异常，请联系管理员检查共享内存状态",
  SHM_ALREADY_EXISTS: "共享内存已存在，请重启 Agent 后重试",
  SHM_NOT_CREATED: "共享内存尚未初始化，请先完成首次接入",
  SHM_SYSCALL_FAILED: "系统资源不足，共享内存操作失败，请联系管理员",
  CONFIG_MISSING_SECTION: "配置文件不完整，请重新描述接入需求",
  CONFIG_PATH_MISSING: "配置文件路径无效，请检查 Agent 部署是否正确",
  DUPLICATE_KEY: "数据点配置冲突，请检查是否有重复的数据点名称",
  UNKNOWN_READER_KEY: "转发配置引用了不存在的数据点，请确认数据点名称正确",
  CONNECTION_REFUSED: "设备连接失败，请确认设备已开机且网络可达",
  TIMEOUT: "设备响应超时，请检查网络连接和设备状态",
  INVALID_CONFIG: "配置参数有误，请检查提交的信息",
  FILE_NOT_FOUND: "配置文件未找到，请联系管理员确认部署",
  PERMISSION_DENIED: "权限不足，请联系管理员",
};

/**
 * McpServiceRegistry — MCP 服务注册表单例。
 *
 * 双层架构：
 * - L1 (服务摘要): `getServiceCatalog()` 返回格式化字符串，注入 SuperWorker 系统提示
 * - L2 (完整定义): `queryRegistry(service_type)` 返回完整 RegistryEntry，step-decomposer 按需拉取
 */
export class McpServiceRegistry {
  private _entries: RegistryEntryValidated[] = [];
  private _errorTranslations: Record<string, string> = {
    ...BUILTIN_ERROR_TRANSLATIONS,
  };
  private _loaded: boolean = false;

  /** 单例实例 */
  private static _instance: McpServiceRegistry | null = null;

  static getInstance(): McpServiceRegistry {
    if (!McpServiceRegistry._instance) {
      McpServiceRegistry._instance = new McpServiceRegistry();
    }
    return McpServiceRegistry._instance;
  }

  /**
   * 重置单例（仅测试用）。
   */
  static resetInstance(): void {
    McpServiceRegistry._instance = null;
  }

  /**
   * 扫描目录，加载并校验所有 Registry JSON 文件。
   *
   * 幂等：重复调用会重新加载并覆盖之前的内容。
   * 单个文件不合法时跳过该文件（agent.md §3.1 用例 1.7），
   * 返回被跳过文件的警告信息。
   *
   * @param dirPath - Registry JSON 文件所在目录路径
   * @returns 被跳过文件的警告信息数组
   */
  async loadFromDirectory(dirPath: string): Promise<string[]> {
    const { entries, warnings } = await loadRegistryFiles(dirPath);

    this._entries = entries;

    // 合并所有 registry 文件的 error_mappings
    this._errorTranslations = { ...BUILTIN_ERROR_TRANSLATIONS };
    for (const entry of entries) {
      for (const [code, msg] of Object.entries(entry.error_mappings)) {
        // 后加载的不会覆盖已存在的翻译（内置优先，先加载的 registry 优先）
        if (!(code in this._errorTranslations)) {
          this._errorTranslations[code] = msg;
        }
      }
    }

    this._loaded = true;
    return warnings;
  }

  /**
   * 返回是否已加载。
   */
  get isLoaded(): boolean {
    return this._loaded;
  }

  /**
   * 获取已加载的 Registry 条目数量。
   */
  get entryCount(): number {
    return this._entries.length;
  }

  /**
   * L1 服务摘要 — 生成格式化字符串，用于注入 SuperWorker 和 plan-generator 系统提示。
   *
   * 格式：
   * ```
   * ## 可用 MCP 服务
   * - **c4_modbus_client** (Modbus 数据采集) [writer]
   *   协议: modbus_tcp — Modbus TCP 协议采集
   *     规则: device.port == 502 → 标准 Modbus TCP 端口
   * ...
   * ```
   *
   * @returns 格式化的服务摘要字符串
   */
  getServiceCatalog(): string {
    if (this._entries.length === 0) {
      return "暂无可用服务。";
    }

    const summaries = this._buildL1Summaries();
    return formatServiceCatalog(summaries);
  }

  /**
   * L1 摘要数组 — 返回结构化摘要，供需要编程式处理的调用者使用。
   */
  getServiceCatalogEntries(): ServiceCatalogEntry[] {
    return this._buildL1Summaries();
  }

  /**
   * L2 完整查询 — 按 service_type 返回完整 RegistryEntry。
   *
   * @param serviceType - 服务类型标识，如 "c4_modbus_client"
   * @returns 完整注册表条目，若未找到返回 null
   */
  queryRegistry(serviceType: string): RegistryEntry | null {
    const entry = this._entries.find(
      (e) => e.service_type === serviceType
    );
    if (!entry) {
      return null;
    }
    return this._toRegistryEntry(entry);
  }

  /** RegistryLookup 兼容接口，供 executor 调用 */
  get_entry(serviceType: string): RegistryEntry | undefined {
    return this.queryRegistry(serviceType) ?? undefined;
  }

  /**
   * 获取合并后的错误翻译表（内置 + 所有 Registry 的 error_mappings）。
   */
  getErrorTranslations(): Readonly<Record<string, string>> {
    return this._errorTranslations;
  }

  /**
   * 获取所有服务类型的列表。
   */
  getServiceTypes(): string[] {
    return this._entries.map((e) => e.service_type);
  }

  // ── 内部方法 ──

  /**
    * 构建 L1 摘要列表（含 point_schema 与 config_schema 字段摘要；
    * 不含 binary_path、error_mappings）。
    */
  private _buildL1Summaries(): ServiceCatalogEntry[] {
    return this._entries.map((entry) => ({
      service_type: entry.service_type,
      display_name: entry.display_name,
      role: entry.role,
      protocols: entry.protocols.map((p) => ({
        protocol: p.protocol,
        description: p.description,
        selection_rules: p.selection_rules.map((r) => ({
          condition: r.condition,
          description: r.description,
        })),
      })),
      point_schema: {
        fields: entry.point_schema.fields.map((f) => ({
          name: f.name,
          type: f.type,
          description: f.description,
        })),
        identity_fields: entry.point_schema.identity_fields
          ? [...entry.point_schema.identity_fields]
          : undefined,
      },
      plan_fields: Object.entries(entry.config_schema.fields)
        .map(([name, field]) => ({
          name,
          type: field.type,
          required: field.default === undefined || field.default === null,
          default: field.default,
          description: field.description,
        })),
      prompt_hints: entry.prompt_hints ? [...entry.prompt_hints] : [],
    }));
  }

  /**
    * 将内部校验类型转换为外部 RegistryEntry 接口。
    */
  private _toRegistryEntry(entry: RegistryEntryValidated): RegistryEntry {
    return {
      service_type: entry.service_type,
      display_name: entry.display_name,
      role: entry.role,
      protocols: entry.protocols,
      point_schema: entry.point_schema,
      config_schema: entry.config_schema,
      binary_path: entry.binary_path,
      prompt_hints: entry.prompt_hints ? [...entry.prompt_hints] : undefined,
      error_mappings: entry.error_mappings,
    };
  }
}

// ── 格式化辅助函数 ──

/**
 * 将 L1 摘要数组格式化为 Markdown 字符串，用于系统提示注入。
 *
 * @param summaries - L1 摘要数组
 * @returns 格式化的 Markdown 字符串
 */
export function formatServiceCatalog(summaries: ServiceCatalogEntry[]): string {
  if (summaries.length === 0) {
    return "暂无可用服务。";
  }

  const lines: string[] = ["## 可用 MCP 服务", ""];

  for (const s of summaries) {
    const roleLabel = s.role === "writer" ? "数据采集" : "数据转发";
    lines.push(
      `- **${s.service_type}** (${s.display_name}) [${roleLabel}/${s.role}]`
    );

    if (s.protocols.length === 0) {
      lines.push("  (无协议信息)");
    } else {
      for (const proto of s.protocols) {
        lines.push(`  协议: \`${proto.protocol}\` — ${proto.description}`);

        if (proto.selection_rules.length > 0) {
          for (const rule of proto.selection_rules) {
            lines.push(
              `    规则: \`${rule.condition}\` → ${rule.description}`
            );
          }
        }
      }
    }

    if (s.point_schema.fields.length > 0) {
      lines.push("  点表字段:");
      for (const f of s.point_schema.fields) {
        lines.push(f.description ? `    ${f.name} (${f.type}) — ${f.description}` : `    ${f.name} (${f.type})`);
      }
    }

    if (s.point_schema.identity_fields && s.point_schema.identity_fields.length > 0) {
      lines.push(
        `  身份字段: ${s.point_schema.identity_fields.join(" + ")}（唯一标识一个点；无点名时按此生成点名）`,
      );
    }

    if (s.plan_fields.length > 0) {
      lines.push("  实例字段:");
      for (const f of s.plan_fields) {
        const req = f.required
          ? "必填（必须由用户提供）"
          : `可选，默认 ${String(f.default)}`;
        lines.push(f.description ? `    ${f.name} (${f.type}, ${req}) — ${f.description}` : `    ${f.name} (${f.type}, ${req})`);
      }
    }

    if (s.prompt_hints.length > 0) {
      lines.push("  使用提示:");
      for (const h of s.prompt_hints) {
        lines.push(`  - ${h}`);
      }
    }

    lines.push("");
  }

  return lines.join("\n");
}

/**
 * 获取 Registry 单例的便捷方法。
 */
export function getRegistry(): McpServiceRegistry {
  return McpServiceRegistry.getInstance();
}
