// c4/agent/src/mcp/tools.ts — MCP 错误翻译层
// 设计：agent.md §3.4 —— 已知错误码在进入 Agent 上下文/用户界面之前做确定性翻译；
// 未匹配的错误码原样透传，由 SuperWorker 兜底规则处理。
// （工具调用传输由 mcp/client.ts 的 Unix-socket MCP 客户端承载。）

import type { McpServiceRegistry } from "../registry/registry.js";

// ── 内置错误翻译（agent.md §3.4）──
export const BUILTIN_ERROR_TRANSLATIONS: Record<string, string> = {
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
