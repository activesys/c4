// c4/agent/src/index.ts — Agent 入口点
// 根据 agent.md §3.2.3 + §5.1 实现启动流程：
//   1. 读取 ~/.local/c4/agent.json → Zod 校验
//   2. McpServiceRegistry.loadFromDirectory()
//   3. 构建 service_catalog → 注入 SuperWorker 系统提示
//   4. 连接 c4_shm_manager，获取 MCP 工具
//   5. createC4Agent（SuperWorker 工厂）
//   6. 启动 Express 服务器
//   7. 启动恢复：create_shm；若 ~/.local/c4/config.json 存在，无条件 Stop-Start

import { readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import * as path from "node:path";
import { createApp } from "./server/app.js";
import { C4McpManager } from "./mcp/client.js";
import {
    McpServiceRegistry,
} from "./registry/registry.js";
import {
    execute_stop_and_start,
    type McpServiceClient,
    type ShmManagerClient,
    type RegistryLookup,
    McpServiceClientAdapter,
    ShmManagerClientAdapter,
    type MCPClientHandle,
} from "./executor/executor.js";
import { MultiServerMCPClient } from "@langchain/mcp-adapters";
import { createC4Agent } from "./super_worker/super_worker.js";
import type {
    AgentConfig,
    SystemConfig,
    MCPInstanceConfig,
    AgentStateSummary as TypesAgentStateSummary,
} from "./types/index.js";
import type { C4Agent, AgentStateProvider } from "./server/types.js";
import { z } from "zod";
import type { StructuredTool } from "@langchain/core/tools";

// ── Agent Config Zod Schema ───────────────────────────────
const AgentConfigSchema: z.ZodType<AgentConfig> = z.object({
    instance_id: z.string(),
    model: z.object({
        provider: z.string(),
        name: z.string(),
        temperature: z.number(),
        max_tokens: z.number(),
        api_key_env: z.string(),
    }),
    server: z.object({
        host: z.string(),
        port: z.number(),
        cors_origin: z.string(),
    }),
    mcp_registry: z.object({
        path: z.string(),
    }),
    shm_manager: z.object({
        binary: z.string(),
        config_path: z.string(),
    }),
    state: z.object({
        backend: z.string(),
        path: z.string(),
    }),
    logging: z.object({
        level: z.string(),
        dir: z.string(),
    }),
});

// ── Agent State Tracker ───────────────────────────────────
/**
 * In-memory agent state tracker.
 *
 * Tracks the current workflow phase and errors for the GET /api/state endpoint.
 * The actual AgentState is managed by LangGraph and written by SuperWorker
 * at key flow points (§3.1).
 */
class AgentStateTracker implements AgentStateProvider {
    private _phase: string = "idle";
    private _hasAccessPlan: boolean = false;
    private _lastError: string | null = null;

    getState(): TypesAgentStateSummary {
        return {
            phase: this._phase as TypesAgentStateSummary["phase"],
            hasAccessPlan: this._hasAccessPlan,
            lastError: this._lastError,
        };
    }

    setPhase(phase: string): void {
        this._phase = phase;
    }

    setAccessPlan(exists: boolean): void {
        this._hasAccessPlan = exists;
    }

    setError(error: string | null): void {
        this._lastError = error;
    }
}

// ── Helpers ───────────────────────────────────────────────

/** 展开路径开头的 ~ 为当前用户主目录（agent.md §5.2 运行时目录位于 ~/.local/c4/） */
function expandHome(p: string): string {
    if (p === "~") {
        return homedir();
    }
    if (p.startsWith("~/")) {
        return path.join(homedir(), p.slice(2));
    }
    return p;
}

/** Logger: simple console-based logger with level filtering. */
class Logger {
    private _level: string;

    constructor(level: string) {
        this._level = level;
    }

    private _shouldLog(level: string): boolean {
        const levels = ["debug", "info", "warn", "error"];
        return levels.indexOf(level) >= levels.indexOf(this._level);
    }

    info(msg: string): void {
        if (this._shouldLog("info")) {
            console.log(`[INFO] ${new Date().toISOString()} ${msg}`);
        }
    }

    warn(msg: string): void {
        if (this._shouldLog("warn")) {
            console.warn(`[WARN] ${new Date().toISOString()} ${msg}`);
        }
    }

    error(msg: string): void {
        if (this._shouldLog("error")) {
            console.error(`[ERROR] ${new Date().toISOString()} ${msg}`);
        }
    }

    debug(msg: string): void {
        if (this._shouldLog("debug")) {
            console.log(`[DEBUG] ${new Date().toISOString()} ${msg}`);
        }
    }
}

// ── Registry Lookup Adapter ───────────────────────────────
class RegistryLookupAdapter implements RegistryLookup {
    constructor(private _registry: McpServiceRegistry) {}

    get_entry(service_type: string) {
        return this._registry.queryRegistry(service_type) ?? undefined;
    }
}

// ── Startup Recovery ──────────────────────────────────────
/**
 * Execute unconditional Stop-Start at startup (§3.2.3).
 *
 * Flow: start c4_shm_manager → create_shm → (if ~/.local/c4/config.json exists)
 * spawn + connect all configured MCP services, then execute stop → adjust_shm → start.
 *
 * If config.json is missing: skip (no data path services to recover).
 *
 * @param config - Agent configuration
 * @param shmManagerBridge - Connected c4_shm_manager bridge
 * @param logger - Logger instance
 */
async function runStartupRecovery(
    config: AgentConfig,
    mcpManager: C4McpManager,
    registry: McpServiceRegistry,
    logger: Logger,
): Promise<void> {
    const configPath = config.shm_manager.config_path;

    const shmClient = new ShmManagerClientAdapter(
        mcpManager.getMultiClient(),
        "shm",
        config.instance_id,
        configPath,
    );

    try {
        const createResult = await shmClient.create_shm();
        if (createResult === "success") {
            logger.info("启动恢复: create_shm 成功，共享内存已创建");
        } else {
            logger.info(`启动恢复: create_shm 结果: ${createResult}（共享内存已存在时属正常）`);
        }
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`启动恢复: create_shm 调用异常: ${msg}`);
    }

    if (!existsSync(configPath)) {
        logger.info("启动恢复: config.json 不存在，跳过（等待首次接入）");
        return;
    }

    const bakPath = configPath + ".bak";

    let systemConfig: SystemConfig;
    try {
        const raw = await readFile(configPath, "utf-8");
        systemConfig = JSON.parse(raw) as SystemConfig;
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`启动恢复: config.json 损坏: ${msg}`);
        if (existsSync(bakPath)) {
            try {
                const bakRaw = await readFile(bakPath, "utf-8");
                systemConfig = JSON.parse(bakRaw) as SystemConfig;
                await writeFile(configPath, bakRaw, "utf-8");
                logger.info("启动恢复: 已从 config.json.bak 恢复 config.json");
            } catch (bakErr: unknown) {
                const bakMsg = bakErr instanceof Error ? bakErr.message : String(bakErr);
                logger.warn(`启动恢复: config.json.bak 也无效: ${bakMsg}，等同首次启动`);
                return;
            }
        } else {
            logger.info("启动恢复: .bak 不存在，等同首次启动");
            return;
        }
    }

    const dataServiceTypes: string[] = [];
    for (const key of Object.keys(systemConfig)) {
        if (key === "c4_shm_manager") continue;
        const instances = systemConfig[key];
        if (Array.isArray(instances) && instances.length > 0) {
            dataServiceTypes.push(key);
        }
    }

    if (dataServiceTypes.length === 0) {
        logger.info("启动恢复: config.json 存在但无数据路径服务，跳过停止/启动");
        try {
            const result = await shmClient.adjust_shm();
            logger.info(`启动恢复: adjust_shm 完成: ${result}`);
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : String(err);
            logger.warn(`启动恢复: adjust_shm 失败: ${msg}`);
        }
        return;
    }

    const registryLookup = new RegistryLookupAdapter(registry);
    const dataClients: McpServiceClient[] = [];
    const tempMultiClients: MultiServerMCPClient[] = [];

    for (const svcType of dataServiceTypes) {
        const entry = registryLookup.get_entry(svcType);
        if (!entry) {
            logger.warn(`启动恢复: 跳过 ${svcType}（Registry 中未找到注册信息）`);
            continue;
        }

        try {
            const multiClient = new MultiServerMCPClient({
                mcpServers: {
                    [svcType]: {
                        transport: "stdio" as const,
                        command: entry.binary_path,
                        args: [],
                    },
                },
            });
            tempMultiClients.push(multiClient);

            const client = new McpServiceClientAdapter(
                null as unknown as MCPClientHandle,
                svcType,
                config.instance_id,
                configPath,
                multiClient,
            );
            dataClients.push(client);
            logger.debug(`启动恢复: 已连接 ${svcType} (${entry.binary_path})`);
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : String(err);
            logger.warn(`启动恢复: 连接 ${svcType} 失败，将跳过: ${msg}`);
        }
    }

    if (dataClients.length === 0) {
        logger.info("启动恢复: 无可用数据路径 MCP 服务");
        for (const mc of tempMultiClients) {
            try { await mc.close(); } catch { /* ignore */ }
        }
        return;
    }

    logger.info(`启动恢复: 无条件执行 Stop-Start（${dataClients.length} 个数据路径服务）`);

    const result = await execute_stop_and_start(
        shmClient,
        dataClients,
        systemConfig,
        configPath,
    );

    if (result.success) {
        logger.info(`启动恢复: Stop-Start 成功，${result.started_services.length} 个服务已启动`);
    } else {
        logger.warn(`启动恢复: Stop-Start 失败: ${result.abort_reason ?? "未知原因"}`);
        if (result.failed_services.length > 0) {
            for (const f of result.failed_services) {
                logger.error(`启动恢复: 服务 ${f.service_type} 启动失败: ${f.error}`);
            }
        }
    }
}

// ── Build Model ────────────────────────────────────────────
/**
 * Create a LangChain chat model based on the provider in agent.json.
 *
 * Currently supports:
 *   - deepseek → @langchain/deepseek ChatDeepSeek
 *
 * Extend with additional providers as needed.
 */
async function createModel(config: AgentConfig, logger: Logger) {
    const { provider, name, temperature, max_tokens, api_key_env } =
        config.model;

    const apiKey = process.env[api_key_env];
    if (!apiKey) {
        throw new Error(
            `环境变量 ${api_key_env} 未设置。请在启动前设置 ${api_key_env}`,
        );
    }

    switch (provider) {
        case "deepseek": {
            const { ChatDeepSeek } = await import("@langchain/deepseek");
            logger.info(`创建模型: deepseek/${name} (temperature=${temperature})`);
            return new ChatDeepSeek({
                apiKey,
                model: name,
                temperature,
                maxTokens: max_tokens,
            });
        }
        default:
            throw new Error(
                `不支持的模型提供商: "${provider}"。当前支持: deepseek`,
            );
    }
}

// ── main ──────────────────────────────────────────────────
async function main(): Promise<void> {
    const configDirArg = process.argv.indexOf("--config-dir");
    const baseDir = expandHome(
        configDirArg >= 0 && configDirArg + 1 < process.argv.length
            ? process.argv[configDirArg + 1]
            : "~/.local/c4",
    );
    const configPath = `${baseDir}/agent.json`;
    let logger = new Logger("info");

    logger.info("C4 Agent 启动中...");

    // ── Step 1: Load config ──
    let rawConfig: string;
    try {
        rawConfig = await readFile(configPath, "utf-8");
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error(`FATAL: 无法读取配置文件 ${configPath}: ${msg}`);
        process.exit(1);
    }

    let config: AgentConfig;
    try {
        const parsed = JSON.parse(rawConfig);
        config = AgentConfigSchema.parse(parsed);
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error(`FATAL: 配置文件 ${configPath} 无效: ${msg}`);
        process.exit(1);
    }

    // 展开配置中各路径的 ~ 前缀（agent.md §5.2）
    config.mcp_registry.path = expandHome(config.mcp_registry.path);
    config.shm_manager.config_path = expandHome(config.shm_manager.config_path);
    config.state.path = expandHome(config.state.path);
    config.logging.dir = expandHome(config.logging.dir);

    // Apply logging level from config
    logger = new Logger(config.logging.level);
    logger.info(
        `配置加载成功: model=${config.model.provider}/${config.model.name}, ` +
        `server=${config.server.host}:${config.server.port}`,
    );

    // ── Step 1.5: Config.json recovery (§3.2.3 step 2) ──
    const dataConfigPath = config.shm_manager.config_path;
    if (existsSync(dataConfigPath)) {
        const bakPath = dataConfigPath + ".bak";
        try {
            const raw = await readFile(dataConfigPath, "utf-8");
            JSON.parse(raw);
        } catch (_err: unknown) {
            logger.warn("config.json 损坏，尝试从 .bak 恢复...");
            if (existsSync(bakPath)) {
                try {
                    const bakRaw = await readFile(bakPath, "utf-8");
                    JSON.parse(bakRaw);
                    await writeFile(dataConfigPath, bakRaw, "utf-8");
                    logger.info("已从 config.json.bak 恢复 config.json");
                } catch (bakErr: unknown) {
                    const bakMsg =
                        bakErr instanceof Error ? bakErr.message : String(bakErr);
                    logger.warn(`config.json.bak 也无效: ${bakMsg}，清空 config.json`);
                    await writeFile(
                        dataConfigPath,
                        JSON.stringify({ c4_shm_manager: { writer: [], reader: [] } }),
                        "utf-8",
                    );
                }
            } else {
                logger.info(".bak 不存在，清空 config.json（等同首次启动）");
                await writeFile(
                    dataConfigPath,
                    JSON.stringify({ c4_shm_manager: { writer: [], reader: [] } }),
                    "utf-8",
                );
            }
        }
    }

    // ── Step 2: Load MCP Service Registry ──
    const registry = McpServiceRegistry.getInstance();
    const registryPath = config.mcp_registry.path;

    try {
        await registry.loadFromDirectory(registryPath);
        logger.info(
            `Registry 加载完成: ${registry.entryCount} 个服务 ` +
            `(路径: ${registryPath})`,
        );
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`Registry 加载有警告: ${msg}`);
        // Non-fatal: agent can still start, just with reduced service catalog
    }

    // ── Step 3: Build system prompt with service catalog ──
    const serviceCatalog = registry.getServiceCatalog();
    const promptTemplatePath = new URL(
        "../src/super_worker/prompts/system.txt",
        import.meta.url,
    ).pathname;

    let systemPrompt: string;
    try {
        const template = await readFile(promptTemplatePath, "utf-8");
        systemPrompt = template.replace("{{ service_catalog }}", serviceCatalog);
        logger.debug("系统提示模板已加载并注入 service_catalog");
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`无法读取系统提示模板: ${msg}`);
        // Fallback: use raw catalog as minimal prompt
        systemPrompt = `你是 C4 Agent。\n\n${serviceCatalog}`;
    }

    // ── Step 4: Setup MCP manager ──
    const mcpManager = new C4McpManager(
        { shm: { binaryPath: config.shm_manager.binary } },
        registry,
    );
    logger.info("MCP manager 已配置（shm_manager + MultiServerMCPClient）");

    // ── Step 5: Build model ──
    let model;
    try {
        model = await createModel(config, logger);
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`FATAL: 无法创建模型: ${msg}`);
        process.exit(1);
    }

    // ── Step 6: Create C4 Agent ──
    let agent: C4Agent;
    try {
        agent = await createC4Agent({
            model,
            registry,
            mcpManager,
            configPath: config.shm_manager.config_path,
            instanceId: config.instance_id,
        });
        logger.info("SuperWorker Agent 已创建");
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`FATAL: 无法创建 SuperWorker: ${msg}`);
        process.exit(1);
    }

    // ── Step 7: Create Agent State Tracker ──
    const stateTracker = new AgentStateTracker();

    // ── Step 8: Create and start Express server ──
    const app = createApp({
        agent,
        stateProvider: stateTracker,
        corsOrigin: config.server.cors_origin,
    });

    const { host, port } = config.server;
    app.listen(port, host, () => {
        logger.info(
            `Express 服务器已启动: http://${host}:${port}`,
        );
        logger.info("C4 Agent 就绪");
    });

    // ── Step 9: Startup Recovery ──
    // Unconditional Stop-Start if /etc/c4/config.json exists (§3.2.3).
    // This runs after the server is listening so the agent is responsive
    // even during recovery.
    await runStartupRecovery(config, mcpManager, registry, logger);

    // ── Graceful Shutdown ──
    const shutdown = async (signal: string) => {
        logger.info(`收到 ${signal}，正在关闭...`);
        try {
            await mcpManager.close();
            logger.info("MCP manager 已关闭");
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : String(err);
            logger.error(`关闭 MCP manager 时出错: ${msg}`);
        }
        process.exit(0);
    };

    process.on("SIGINT", () => shutdown("SIGINT"));
    process.on("SIGTERM", () => shutdown("SIGTERM"));
}

// ── Run ───────────────────────────────────────────────────
main().catch((err: unknown) => {
    console.error("FATAL: unhandled startup error:", err);
    process.exit(1);
});
