// c4/agent/src/mcp/tools.ts — MCP 工具转换器 + 错误翻译层
// 根据 agent.md §3.4 实现
// convertMcpTool: MCP Tool → LangChain StructuredTool
// translateError: MCP 错误码 → 自然语言描述

import { StructuredTool } from "@langchain/core/tools";
import { z } from "zod";
import type { Tool } from "@modelcontextprotocol/sdk/types.js";
import type { McpClientBridge } from "./bridge.js";
import type { McpServiceRegistry } from "../registry/registry.js";

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
  SERVICE_NOT_READY: "服务尚未就绪，请稍后再试",
  INVALID_CONFIG: "配置参数有误，请检查提交的信息",
  FILE_NOT_FOUND: "配置文件未找到，请联系管理员确认部署",
  PERMISSION_DENIED: "权限不足，请联系管理员",
};

// ── JSON Schema 属性类型 → Zod 类型映射 ──
const JSON_SCHEMA_TYPE_TO_ZOD: Record<string, () => z.ZodTypeAny> = {
  string: () => z.string(),
  number: () => z.number(),
  integer: () => z.number().int(),
  boolean: () => z.boolean(),
  array: () => z.array(z.unknown()),
  object: () => z.record(z.string(), z.unknown()),
};

/**
 * 将 MCP Tool 的 JSON Schema 转换为 Zod schema。
 *
 * 处理顶层 `{ type: "object", properties, required }` 结构。
 *
 * @param inputSchema - MCP Tool 的 inputSchema
 * @returns Zod schema 对象
 */
function inputSchemaToZod(inputSchema: Tool["inputSchema"]): z.ZodObject<z.ZodRawShape> {
  const properties = (inputSchema.properties ?? {}) as Record<
    string,
    { type?: string; description?: string; [key: string]: unknown }
  >;
  const required = new Set(inputSchema.required ?? []);
  const shape: Record<string, z.ZodTypeAny> = {};

  for (const [key, prop] of Object.entries(properties)) {
    let zodType: z.ZodTypeAny = z.unknown();

    if (prop.type && typeof prop.type === "string") {
      const factory = JSON_SCHEMA_TYPE_TO_ZOD[prop.type];
      if (factory) {
        zodType = factory();
      }
    }

    // 为每个字段添加 description
    if (prop.description && typeof prop.description === "string") {
      zodType = zodType.describe(prop.description);
    }

    // 非 require 字段设为 optional
    if (!required.has(key)) {
      zodType = zodType.optional();
    }

    shape[key] = zodType;
  }

  return z.object(shape as z.ZodRawShape);
}

/**
 * convertMcpTool — 将 MCP Tool 转换为 LangChain StructuredTool。
 *
 * 在执行结果进入 Agent 上下文之前，对已知错误码做确定性翻译。
 * 未匹配的错误码原样透传，由 SuperWorker 兜底规则处理。
 *
 * @param mcpTool - MCP SDK 返回的 Tool 定义
 * @param bridge - 已连接的 McpClientBridge
 * @param errorTranslations - 合并后的错误翻译表
 * @returns LangChain StructuredTool
 */
export function convertMcpTool(
  mcpTool: Tool,
  bridge: McpClientBridge,
  errorTranslations: Record<string, string> = {}
): StructuredTool {
  const mergedTranslations: Record<string, string> = {
    ...BUILTIN_ERROR_TRANSLATIONS,
    ...errorTranslations,
  };

  class McpStructuredTool extends StructuredTool {
    name = mcpTool.name;
    description = mcpTool.description ?? `MCP tool: ${mcpTool.name}`;
    schema = inputSchemaToZod(mcpTool.inputSchema);

    async _call(
      input: Record<string, unknown>
    ): Promise<string> {
      try {
        const result = await bridge.callTool({
          name: mcpTool.name,
          arguments: input,
        });

        // 提取文本内容
        return extractToolResultText(result, mergedTranslations);
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        // 翻译已知错误码
        const translated = translateError(message, mergedTranslations);

        // 构造友好的错误信息
        const errorMsg = translated !== message
          ? `操作失败: ${translated}`
          : `操作失败: ${message}`;

        // 重新抛出，让 LangChain 框架处理
        throw new Error(errorMsg);
      }
    }
  }

  return new McpStructuredTool();
}

/**
 * 将 McpClientBridge 的所有工具批量转换为 LangChain StructuredTool。
 *
 * @param bridge - 已连接的 McpClientBridge
 * @param errorTranslations - 合并后的错误翻译表（可选，可从 Registry 获取）
 * @returns StructuredTool 数组
 */
export function convertMcpTools(
  bridge: McpClientBridge,
  errorTranslations: Record<string, string> = {}
): StructuredTool[] {
  return bridge.tools.map((tool) =>
    convertMcpTool(tool, bridge, errorTranslations)
  );
}

/**
 * 从 McpServiceRegistry 构建合并的错误翻译表。
 *
 * 与 BUILTIN_ERROR_TRANSLATIONS 合并，registry 的 error_mappings 不覆盖内置条目。
 *
 * @param registry - McpServiceRegistry 实例
 * @returns 合并后的错误翻译表
 */
export function buildErrorTranslator(
  registry: McpServiceRegistry
): Record<string, string> {
  return {
    ...BUILTIN_ERROR_TRANSLATIONS,
    ...registry.getErrorTranslations(),
  };
}

/**
 * translateError — 对单条文本中出现的已知错误码进行确定性翻译。
 *
 * 遍历错误翻译表，若文本包含已知错误码字符串，返回对应翻译。
 * 未匹配的错误码原样返回，由 SuperWorker 的兜底规则处理。
 *
 * @param text - 原始错误文本（可能包含错误码）
 * @param translations - 错误码 → 自然语言翻译表
 * @returns 翻译后的文本
 */
export function translateError(
  text: string,
  translations: Record<string, string> = {}
): string {
  const merged: Record<string, string> = {
    ...BUILTIN_ERROR_TRANSLATIONS,
    ...translations,
  };

  for (const [code, msg] of Object.entries(merged)) {
    if (text.includes(code)) {
      return msg;
    }
  }

  // 未匹配的错误码原样透传
  return text;
}

/**
 * 从 MCP CallToolResult 中提取文本内容，并对已知错误码做翻译。
 *
 * @param result - MCP callTool 返回结果
 * @param translations - 错误翻译表
 * @returns 提取并翻译后的文本
 */
function extractToolResultText(
  result: unknown,
  translations: Record<string, string>
): string {
  const content = (result as { content?: Array<{ type: string; text?: string }> })?.content;
  const textParts: string[] = [];

  if (Array.isArray(content)) {
    for (const item of content) {
      if (item.type === "text" && typeof item.text === "string") {
        textParts.push(item.text);
      }
    }
  }

  const raw = textParts.length > 0
    ? textParts.join("\n")
    : JSON.stringify(content ?? result);

  // 对输出文本中的已知错误码做翻译
  return translateError(raw, translations);
}
