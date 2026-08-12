// c4/agent/src/mcp/client.ts — MCP 客户端管理
// 使用 @langchain/mcp-adapters 的 MultiServerMCPClient，替代自建 bridge + tool 转换

import { MultiServerMCPClient } from "@langchain/mcp-adapters";
import type { StructuredTool } from "@langchain/core/tools";
import type { McpServiceRegistry } from "../registry/registry.js";

// ── 错误翻译（保留中文本地化）─────────────────────────────

const BUILTIN_ERRORS: Record<string, string> = {
    SHM_CORRUPTED: "数据存储异常，请联系管理员检查共享内存状态",
    SHM_SYSCALL_FAILED: "系统资源不足，共享内存操作失败",
    CONNECTION_REFUSED: "设备连接失败，请确认设备已开机且网络可达",
    TIMEOUT: "设备响应超时，请检查网络连接",
    INVALID_CONFIG: "配置参数有误，请检查提交的信息",
};

function buildErrorTranslator(registry: McpServiceRegistry): Record<string, string> {
    const merged = { ...BUILTIN_ERRORS };
    if (registry.isLoaded) {
        for (const entry of registry.getServiceCatalogEntries()) {
            const full = registry.queryRegistry(entry.service_type);
            if (full?.error_mappings) {
                Object.assign(merged, full.error_mappings);
            }
        }
    }
    return merged;
}

function translateError(message: string, translations: Record<string, string>): string {
    for (const [code, msg] of Object.entries(translations)) {
        if (message.includes(code)) return msg;
    }
    return message;
}

// ── MCP 服务配置 ──────────────────────────────────────────

export interface McpServerConfig {
    binaryPath: string;
    args?: string[];
}

// ── C4McpManager ──────────────────────────────────────────

export class C4McpManager {
    private _client: MultiServerMCPClient;
    private _errorTranslations: Record<string, string>;
    private _tools: StructuredTool[] = [];

    constructor(
        servers: Record<string, McpServerConfig>,
        registry: McpServiceRegistry,
    ) {
        const serverConfigs: Record<string, { transport: "stdio"; command: string; args: string[] }> = {};
        for (const [name, cfg] of Object.entries(servers)) {
            serverConfigs[name] = {
                transport: "stdio",
                command: cfg.binaryPath,
                args: cfg.args ?? [],
            };
        }
        this._client = new MultiServerMCPClient(serverConfigs);
        this._errorTranslations = buildErrorTranslator(registry);
    }

    /** 获取所有工具的 LangChain StructuredTool 数组（带错误翻译） */
    async getTools(): Promise<StructuredTool[]> {
        const rawTools = await this._client.getTools();
        const translations = this._errorTranslations;

        const wrapError = (name: string, fn: () => Promise<string>): Promise<string> => {
            return fn().catch((err: unknown) => {
                const msg = err instanceof Error ? err.message : String(err);
                const translated = translateError(msg, translations);
                throw new Error(translated !== msg ? `操作失败: ${translated}` : `操作失败: ${msg}`);
            });
        };

        this._tools = rawTools.map((t) => {
            const origInvoke = (t as any).invoke?.bind(t) || (t as any)._call?.bind(t);
            if (origInvoke) {
                const wrapped = { ...t, invoke: (input: any) => wrapError(t.name, () => origInvoke(input)) };
                return wrapped as unknown as StructuredTool;
            }
            return t as unknown as StructuredTool;
        });

        return this._tools;
    }

    /** 获取底层 MCP client（用于 stop/start 等直接调用） */
    getClient(name: string): unknown {
        return this._client.getClient(name);
    }

    /** 获取底层 MultiServerMCPClient（用于 callToolViaMultiClient 等 LangChain 工具调用） */
    getMultiClient(): MultiServerMCPClient {
        return this._client;
    }

    /** 断开所有连接 */
    async close(): Promise<void> {
        await this._client.close();
    }
}
