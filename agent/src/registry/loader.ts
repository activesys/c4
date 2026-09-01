// c4/agent/src/registry/loader.ts — Registry JSON 目录扫描与 Zod 校验
// 根据 agent.md §3.3 实现

import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { z } from "zod";

// ── RegistryFieldSchema：config_schema 中每个字段的定义 ──
const RegistryFieldSchema = z.object({
  type: z.string(),
  source: z.enum(["plan", "default"]),
  default: z.unknown().nullable().optional(),
  description: z.string(),
});

// ── RegistrySelectionRuleSchema ──
const RegistrySelectionRuleSchema = z.object({
  condition: z.string(),
  description: z.string(),
});

// ── RegistryProtocolSchema ──
const RegistryProtocolSchema = z.object({
  protocol: z.string(),
  description: z.string(),
  selection_rules: z.array(RegistrySelectionRuleSchema),
});

// ── RegistryConfigSchemaFieldMap：config_schema.fields 是 Record<string, RegistryField> ──
const RegistryConfigSchemaFieldMap = z.record(z.string(), RegistryFieldSchema);

// ── RegistryConfigSchemaSchema ──
const RegistryConfigSchemaSchema = z.object({
  required: z.array(z.string()).optional(),
  fields: RegistryConfigSchemaFieldMap,
});

// ── PointFieldSchema：point_schema.fields 中每个字段的定义 ──
const PointFieldSchema = z.object({
  name: z.string(),
  type: z.string(),
  description: z.string(),
});

// ── PointSchemaSchema：point_schema（agent.md §3.3）──
// fields = 点表业务字段；identity_fields = 身份字段（Writer 必填，Reader 不填）
const PointSchemaSchema = z.object({
  fields: z.array(PointFieldSchema),
  identity_fields: z.array(z.string()).optional(),
});

// ── RegistryEntrySchema：Registry JSON 的 Zod 校验 ──
const RegistryEntrySchema = z.object({
  service_type: z.string(),
  display_name: z.string(),
  role: z.enum(["writer", "reader"]),
  protocols: z.array(RegistryProtocolSchema),
  point_schema: PointSchemaSchema,
  config_schema: RegistryConfigSchemaSchema,
  binary_path: z.string(),
  prompt_hints: z.array(z.string()).optional(),
  error_mappings: z.record(z.string(), z.string()),
});

// prompt_hints 软护栏（超限条目跳过并计入 warnings，不导致加载失败）
const PROMPT_HINTS_MAX_PER_SERVICE = 5;
const PROMPT_HINTS_MAX_CHARS = 200;

/** 返回合规的提示条目；超限条目以 warning 形式上报 */
function sanitize_prompt_hints(
  fileName: string,
  hints: string[] | undefined,
  warnings: string[],
): string[] {
  if (!hints) return [];
  const kept: string[] = [];
  for (const hint of hints) {
    if (kept.length >= PROMPT_HINTS_MAX_PER_SERVICE) {
      warnings.push(
        `${fileName}: prompt_hints 超过 ${PROMPT_HINTS_MAX_PER_SERVICE} 条上限，已忽略多余条目`
      );
      break;
    }
    if (typeof hint !== "string" || hint.length === 0) continue;
    if (hint.length > PROMPT_HINTS_MAX_CHARS) {
      warnings.push(
        `${fileName}: prompt_hints 单条超过 ${PROMPT_HINTS_MAX_CHARS} 字符上限，已忽略该条`
      );
      continue;
    }
    kept.push(hint);
  }
  return kept;
}

export type RegistryEntryValidated = z.infer<typeof RegistryEntrySchema>;

/** 加载结果：有效条目 + 被跳过文件的警告信息（agent.md §3.1 用例 1.7：
 *  单个文件损坏不导致全局加载失败，跳过并告警）。 */
export interface RegistryLoadResult {
  entries: RegistryEntryValidated[];
  warnings: string[];
}

// 加载期校验（agent.md §3.3）：role=writer 必须声明非空 identity_fields，
// 且每个条目必须是 fields 中已声明的字段名；不满足则 Registry 加载报错。
function semantic_error(fileName: string, entry: RegistryEntryValidated): string | null {
  if (entry.role !== "writer") {
    return null;
  }
  const identityFields = entry.point_schema.identity_fields;
  if (!identityFields || identityFields.length === 0) {
    return `${fileName}: role=writer 的条目必须声明非空 point_schema.identity_fields`;
  }
  const declared = new Set(entry.point_schema.fields.map((f) => f.name));
  for (const f of identityFields) {
    if (!declared.has(f)) {
      return `${fileName}: point_schema.identity_fields 中的 "${f}" 不是 fields 中已声明的字段名`;
    }
  }
  return null;
}

/**
 * 从目录中加载所有 Registry JSON 文件。
 *
 * 单个文件解析失败或校验不通过时跳过该文件并记录警告，
 * 不影响其余有效文件的加载（agent.md §3.1 用例 1.7）。
 *
 * @param dirPath - Registry JSON 文件所在目录路径
 * @returns 有效条目与警告信息
 * @throws 仅当目录不可读（非 ENOENT）时抛出
 */
export async function loadRegistryFiles(dirPath: string): Promise<RegistryLoadResult> {
  let dirEntries: string[];

  try {
    dirEntries = await readdir(dirPath);
  } catch (err: unknown) {
    const error = err as NodeJS.ErrnoException;
    if (error.code === "ENOENT") {
      return { entries: [], warnings: [] };
    }
    throw new Error(
      `Failed to read Registry directory "${dirPath}": ${error.message}`
    );
  }

  const jsonFiles = dirEntries.filter((name) => name.endsWith(".json"));
  if (jsonFiles.length === 0) {
    return { entries: [], warnings: [] };
  }

  const entries: RegistryEntryValidated[] = [];
  const warnings: string[] = [];

  for (const fileName of jsonFiles) {
    const filePath = join(dirPath, fileName);
    let raw: string;

    try {
      raw = await readFile(filePath, "utf-8");
    } catch (err: unknown) {
      const error = err as NodeJS.ErrnoException;
      warnings.push(`${fileName}: failed to read — ${error.message}`);
      continue;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (err: unknown) {
      const error = err as Error;
      warnings.push(`${fileName}: invalid JSON — ${error.message}`);
      continue;
    }

    const result = RegistryEntrySchema.safeParse(parsed);
    if (!result.success) {
      const issues = result.error.issues
        .map((i) => `  - ${i.path.join(".")}: ${i.message}`)
        .join("\n");
      warnings.push(`${fileName}: schema validation failed\n${issues}`);
      continue;
    }

    const semantic = semantic_error(fileName, result.data);
    if (semantic) {
      warnings.push(semantic);
      continue;
    }

    const entry = result.data;
    if (entry.prompt_hints) {
      entry.prompt_hints = sanitize_prompt_hints(fileName, entry.prompt_hints, warnings);
    }
    entries.push(entry);
  }

  return { entries, warnings };
}
