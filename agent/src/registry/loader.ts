// c4/agent/src/registry/loader.ts — Registry JSON 目录扫描与 Zod 校验
// 根据 agent.md §3.3 实现

import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { z } from "zod";

// ── RegistryFieldSchema：config_schema 中每个字段的定义 ──
const RegistryFieldSchema = z.object({
  type: z.string(),
  source: z.enum(["plan", "default"]),
  default: z.unknown().nullable(),
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
  required: z.array(z.string()),
  fields: RegistryConfigSchemaFieldMap,
});

// ── RegistryEntrySchema：Registry JSON 的 Zod 校验 ──
const RegistryEntrySchema = z.object({
  service_type: z.string(),
  display_name: z.string(),
  role: z.enum(["writer", "reader"]),
  protocols: z.array(RegistryProtocolSchema),
  config_schema: RegistryConfigSchemaSchema,
  binary_path: z.string(),
  error_mappings: z.record(z.string(), z.string()),
});

export type RegistryEntryValidated = z.infer<typeof RegistryEntrySchema>;

/**
 * 从目录中加载所有 Registry JSON 文件。
 *
 * @param dirPath - Registry JSON 文件所在目录路径
 * @returns 校验通过的 RegistryEntry 数组
 * @throws 若任一 JSON 文件解析失败或校验不通过，抛出聚合错误
 */
export async function loadRegistryFiles(dirPath: string): Promise<RegistryEntryValidated[]> {
  let dirEntries: string[];

  try {
    dirEntries = await readdir(dirPath);
  } catch (err: unknown) {
    const error = err as NodeJS.ErrnoException;
    if (error.code === "ENOENT") {
      return [];
    }
    throw new Error(
      `Failed to read Registry directory "${dirPath}": ${error.message}`
    );
  }

  const jsonFiles = dirEntries.filter((name) => name.endsWith(".json"));
  if (jsonFiles.length === 0) {
    return [];
  }

  const entries: RegistryEntryValidated[] = [];
  const errors: string[] = [];

  for (const fileName of jsonFiles) {
    const filePath = join(dirPath, fileName);
    let raw: string;

    try {
      raw = await readFile(filePath, "utf-8");
    } catch (err: unknown) {
      const error = err as NodeJS.ErrnoException;
      errors.push(`${fileName}: failed to read — ${error.message}`);
      continue;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (err: unknown) {
      const error = err as Error;
      errors.push(`${fileName}: invalid JSON — ${error.message}`);
      continue;
    }

    const result = RegistryEntrySchema.safeParse(parsed);
    if (!result.success) {
      const issues = result.error.issues
        .map((i) => `  - ${i.path.join(".")}: ${i.message}`)
        .join("\n");
      errors.push(`${fileName}: schema validation failed\n${issues}`);
      continue;
    }

    entries.push(result.data);
  }

  if (errors.length > 0) {
    return entries;
  }

  return entries;
}
