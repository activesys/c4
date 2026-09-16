// c4/agent/src/index.ts — Agent 入口点
// 根据 agent.md §3.2.3 + c4_architecture.md §3.1.2 实现启动流程：
//   1. 读取 ~/.local/c4/agent.json → Zod 校验
//   2. McpServiceRegistry.loadFromDirectory()
//   3. 构建 service_catalog → 注入 SuperWorker 系统提示
//   4. C4McpManager：连 Unix socket（env C4_SOCK_DIR，默认 /run/c4）——从不拉起 MCP 进程
//   5. createC4Agent（SuperWorker 工厂）
//   6. 启动 Express 服务器
//   7. 四级瀑布启动恢复：L0 config 健康 → L1 连接 → L2 收敛 → L3 监控接续
//      （收敛不做全量 Stop-Start：无在途事务标记时仅 start，ALREADY_RUNNING 无动作）

import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import * as path from "node:path";
import { createApp } from "./server/app.js";
import {
    C4McpManager,
    SHM_SERVICE_TYPE,
    resolveSockDir,
} from "./mcp/client.js";
import {
    McpServiceRegistry,
} from "./registry/registry.js";
import {
    load_abbr_registry,
    save_abbr_registry,
} from "./registry/abbr_registry.js";
import {
    execute_stop_and_start,
    data_service_types,
    McpServiceClientAdapter,
    ShmManagerClientAdapter,
    is_success_result,
} from "./executor/executor.js";
import { translateError } from "./mcp/tools.js";
import {
    load_valid_config,
    read_pending_marker,
    restore_prev1,
    clear_pending_marker,
} from "./executor/transaction.js";
import { with_config_lock } from "./executor/single_flight.js";
import { createC4Agent } from "./super_worker/super_worker.js";
import { DisplayService } from "./display/session.js";
import { createDisplayRouter } from "./display/routes.js";
import { createDisplayTools } from "./display/tools.js";
import { AgentLogger, type AgentLogLevel } from "./logging/agent_logger.js";
import type {
    AgentConfig,
    SystemConfig,
    AgentPhase,
    AgentStateSummary as TypesAgentStateSummary,
} from "./types/index.js";
import type { C4Agent, AgentStateProvider, AgentStateWriter } from "./server/types.js";
import { z } from "zod";

// ── Agent Config Zod Schema ───────────────────────────────
const AgentConfigSchema: z.ZodType<AgentConfig> = z.object({
    instance_id: z.string(),
    model: z.object({
        provider: z.string(),
        name: z.string(),
        base_url: z.string(),
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
        agent_level: z.string().optional(),
    }),
    frontend: z.object({
        dir: z.string(),
    }).optional(),
    site: z.object({
        name: z.string(),
        abbr: z.string(),
    }).optional(),
    display: z.object({
        stale_threshold_ms: z.number().int().optional(),
    }).optional(),
});

// ── Agent State Tracker ───────────────────────────────────
/**
 * In-memory agent state tracker.
 *
 * Tracks the current workflow phase and errors for the GET /api/state endpoint.
 * The actual AgentState is managed by LangGraph and written by SuperWorker
 * at key flow points (§3.1).
 */
class AgentStateTracker implements AgentStateProvider, AgentStateWriter {
    private _phase: AgentPhase = "idle";
    private _hasAccessPlan: boolean = false;
    private _lastError: string | null = null;

    getState(): TypesAgentStateSummary {
        return {
            phase: this._phase,
            hasAccessPlan: this._hasAccessPlan,
            lastError: this._lastError,
        };
    }

    setPhase(phase: AgentPhase): void {
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
class RegistryLookupAdapter {
    constructor(private _registry: McpServiceRegistry) {}

    get_entry(service_type: string) {
        return this._registry.queryRegistry(service_type) ?? undefined;
    }

    service_types(): string[] {
        return this._registry.getServiceTypes();
    }
}

// ── Four-Level Startup Waterfall（c4_architecture.md §3.1.2）──

/** c4_shm_manager socket 等待上限（L1 硬前置挂起点；超时报告后放弃数据路径收敛） */
const SHM_WAIT_TIMEOUT_MS = 300_000;

/**
 * 四级瀑布：L0 config 健康 → L1 连接 → L2 收敛 → L3 监控接续。
 *
 * 收敛按差异最小动作：无在途事务标记时仅 start（ALREADY_RUNNING 无动作，
 * 零中断）；只有涉及已回滚事务的服务才执行完整 Stop-Start（stop → adjust_shm →
 * start，以恢复后的配置执行，确定性全量重载）。
 *
 * 全程持有配置事务单飞锁——瀑布收敛期间到达的配置变更请求在会话层被拒绝。
 */
async function runStartupWaterfall(
    config: AgentConfig,
    mcpManager: C4McpManager,
    registry: McpServiceRegistry,
    logger: Logger,
    stateTracker: AgentStateWriter,
): Promise<void> {
    try {
        await with_config_lock(async () => {
            const configPath = config.shm_manager.config_path;
            const registryLookup = new RegistryLookupAdapter(registry);
            let l0Report: string | null = null;
            let rollbackServices: Set<string> | null = null;

            // ── L0: config.json 健康（parse + schema）──
            let systemConfig: SystemConfig | null = null;
            const marker = await read_pending_marker(configPath);
            if (marker !== null) {
                // 在途事务标记 → 已开始的变更一律作废回滚、不续做（C4_RS_00066）
                const restored = await restore_prev1(configPath);
                await clear_pending_marker(configPath);
                if (restored) {
                    l0Report = "上次接入变更未完成，已回滚，接入不成功";
                    logger.warn(`L0: ${l0Report}（涉及服务: ${marker.services.join(", ") || "全部"}）`);
                    // 涉及已回滚事务的服务 → 完整 Stop-Start；标记未列明服务 → 全部
                    rollbackServices = new Set(
                        marker.services.length > 0
                            ? marker.services
                            : ["*"],
                    );
                } else {
                    // .prev 不可用（损坏/缺失）→ 不得覆盖 config.json，报告异常等待人工介入；
                    // 首次接入尚无 .prev 时崩溃于 rename 之后 → 保留新 config.json，
                    // 报告「上次变更结果未知，请核验」
                    l0Report = existsSync(`${configPath}.prev.1`)
                        ? "上次接入变更出现异常，配置未能恢复，系统等待人工介入"
                        : "上次接入变更结果未知，请核验当前配置";
                    logger.error(`L0: 恢复 .prev.1 失败——保留当前 config.json，${l0Report}`);
                }
            }

            if (existsSync(configPath)) {
                systemConfig = await load_valid_config(configPath);
                if (systemConfig === null) {
                    // config.json 损坏（无标记）→ 恢复 .prev → 报告接入不成功
                    const restored = await restore_prev1(configPath);
                    if (restored) {
                        systemConfig = await load_valid_config(configPath);
                        l0Report = l0Report ?? "检测到配置损坏，已恢复到上一版本";
                        logger.warn(`L0: config.json 损坏，已从 config.json.prev.1 恢复`);
                    } else {
                        l0Report =
                            "配置文件损坏且无法恢复，系统等待人工介入（保留现状，未做任何修改）";
                        logger.error(`L0: config.json 损坏且 .prev.1 不可用——${l0Report}`);
                    }
                }
                if (systemConfig !== null) {
                    logger.info("L0: config.json 健康（parse + schema 通过），获得权威地位（期望状态声明）");
                }
            } else {
                logger.info("L0: config.json 不存在——等待首次接入（无数据路径服务）");
            }

            // ── L1: 连接（逐服务连 Unix socket + MCP initialize；只连接、从不拉起进程）──
            await mcpManager.connectAll();
            for (const st of mcpManager.serviceTypes()) {
                if (mcpManager.isConnected(st)) {
                    logger.debug(`L1: 已连接 ${st}`);
                } else {
                    logger.warn(`L1: ${st} 暂不可连，标记降级、退避重试（不阻塞其余服务）`);
                }
            }
            // c4_shm_manager 是唯一的全局前置：socket 不可连时挂起全部数据服务收敛、
            // 退避等待（不得以 SHM_OPEN_FAILED 告警风暴的形式失败）
            const shmReady = await mcpManager.waitShmConnected(SHM_WAIT_TIMEOUT_MS);
            if (!shmReady) {
                l0Report =
                    l0Report ??
                    "基础数据服务连接超时，数据接入暂时不可用，请检查部署后重启";
                logger.error(`L1: ${SHM_SERVICE_TYPE} 在 ${SHM_WAIT_TIMEOUT_MS}ms 内不可连——放弃本轮数据路径收敛`);
            } else {
                logger.info("L1: 连接完成（c4_shm_manager 就绪）");
            }

            // ── L2: 收敛（信任 MCP 契约返回，不做独立的状态探测）──
            if (shmReady) {
                // 对账 shm：不存在则 create_shm（幂等 create-or-attach，agent.md §3.2.3；
                // SHM_CORRUPTED → 拒绝并报告，不做自愈——c4_deployment.md §6.3）
                const shmClient = new ShmManagerClientAdapter(
                    mcpManager,
                    config.instance_id,
                    configPath,
                );
                try {
                    const createResult = await shmClient.create_shm();
                    if (is_success_result(createResult)) {
                        logger.info("L2: create_shm 完成（幂等 create-or-attach）");
                    } else {
                        logger.error(`L2: create_shm 失败: ${createResult}`);
                        l0Report = l0Report ?? translateError(createResult);
                    }
                } catch (err: unknown) {
                    const msg = err instanceof Error ? err.message : String(err);
                    logger.error(`L2: create_shm 调用异常: ${msg}`);
                    l0Report = l0Report ?? translateError(msg);
                }

                if (systemConfig !== null) {
                    const involvedAll = rollbackServices?.has("*") ?? false;
                    for (const svcType of data_service_types(systemConfig)) {
                        if (!mcpManager.isConnected(svcType)) {
                            // 记录失败并保持降级，继续处理其余服务（§3.2.3 L2 / C4_RS_00242）
                            logger.warn(`L2: ${svcType} 降级中，本轮跳过（记录失败，继续处理其余服务）`);
                            l0Report = l0Report ??
                                "部分数据服务暂时无法连接，已记录，待其恢复后自动接入";
                            continue;
                        }
                        if (!registryLookup.get_entry(svcType)) {
                            logger.warn(`L2: 跳过 ${svcType}（Registry 中未找到注册信息）`);
                            continue;
                        }
                        const client = new McpServiceClientAdapter(
                            mcpManager,
                            svcType,
                            config.instance_id,
                            configPath,
                        );
                        try {
                            if (rollbackServices !== null && (involvedAll || rollbackServices.has(svcType))) {
                                // 涉及已回滚事务的服务 → 完整 Stop-Start（确定性全量重载）
                                logger.info(`L2: ${svcType} 涉及已回滚事务，执行完整 Stop-Start`);
                                const result = await execute_stop_and_start(
                                    shmClient,
                                    [client],
                                    [client],
                                    systemConfig,
                                    configPath,
                                );
                                if (result.success) {
                                    logger.info(`L2: ${svcType} Stop-Start 完成`);
                                } else {
                                    logger.error(`L2: ${svcType} Stop-Start 失败: ${result.abort_reason}`);
                                    l0Report = l0Report ?? translateError(result.abort_reason ?? "");
                                }
                                continue;
                            }
                            const result = await client.start();
                            if (result === "success") {
                                logger.info(`L2: ${svcType} start 成功（此前为空白进程，实例已按当前配置拉起）`);
                            } else {
                                logger.error(`L2: ${svcType} start 失败: ${result}`);
                                l0Report = l0Report ?? translateError(result);
                            }
                        } catch (err: unknown) {
                            const msg = err instanceof Error ? err.message : String(err);
                            logger.error(`L2: ${svcType} 收敛调用异常: ${msg}`);
                            l0Report = l0Report ?? translateError(msg);
                        }
                    }
                }
            }

            // ── L3: 监控接续 ──
            // 重建周期监控；服务存活状态＝连接状态推导（供页面展示与告警）——
            // 由 C4McpManager 连接状态与 DisplayService 周期轮询承载
            logger.info("L3: 监控接续（存活状态＝连接状态推导），瀑布收敛完成，Agent 就绪");

            if (l0Report !== null) {
                // 显式告知用户，不得静默（§3.1.2 崩溃恢复语义 / C4_RS_00066）
                stateTracker.setError(l0Report);
            }
        });
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`启动瀑布异常: ${msg}`);
        stateTracker.setError(`启动恢复出现问题: ${translateError(msg)}`);
    }
}

// ── Build Model ────────────────────────────────────────────
/**
 * 基于 agent.json 配置的 OpenAI 兼容端点构建模型；provider 仅作标识。
 *
 * 任何兼容 OpenAI chat/completions 协议的端点均可接入：
 * base_url 指向端点，name 为模型 ID，api_key_env 指定密钥环境变量。
 */
async function createModel(config: AgentConfig, logger: Logger) {
    const { name, base_url, temperature, max_tokens, api_key_env } =
        config.model;

    const apiKey = process.env[api_key_env];
    if (!apiKey) {
        throw new Error(
            `环境变量 ${api_key_env} 未设置。请在启动前设置 ${api_key_env}`,
        );
    }

    const { ChatOpenAI } = await import("@langchain/openai");
    logger.info(`创建模型: ${name} @ ${base_url} (temperature=${temperature})`);
    return new ChatOpenAI({
        apiKey,
        model: name,
        temperature,
        maxTokens: max_tokens,
        configuration: {
            baseURL: base_url,
        },
    });
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
    if (config.frontend) {
        config.frontend.dir = expandHome(config.frontend.dir);
    }

    // Apply logging level from config
    logger = new Logger(config.logging.level);

    // 进程级兜底（独立服务模型，c4_architecture.md §3.1.1）：Agent 是常驻系统服务，
    // 对话层异步链路的未观察拒绝（如图运行中止的 GraphRecursionError）不得使
    // 进程退出——崩溃会中断 Web/对话能力，而 MCP 数据路径并不因此受益。
    process.on("unhandledRejection", (reason: unknown) => {
        const msg = reason instanceof Error ? reason.message : String(reason);
        logger.error(`未处理的 Promise 拒绝（进程保持运行）: ${msg}`);
    });
    logger.info(
        `配置加载成功: model=${config.model.provider}/${config.model.name}, ` +
        `server=${config.server.host}:${config.server.port}`,
    );

    // Agent 运行日志（第二层：结构化 NDJSON → logging.dir）
    const agentLogger = new AgentLogger(
        config.logging.dir,
        (config.logging.agent_level ?? "debug") as AgentLogLevel,
    );

    // ── Step 2: Load MCP Service Registry ──
    const registry = McpServiceRegistry.getInstance();
    const registryPath = config.mcp_registry.path;

    try {
        const warnings = await registry.loadFromDirectory(registryPath);
        if (warnings.length > 0) {
            logger.warn(
                `Registry 跳过 ${warnings.length} 个不合法文件:\n${warnings.join("\n")}`
            );
        }
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
        // eslint-disable-next-line @typescript-eslint/no-unused-vars -- systemPrompt 构造后未注入任何 agent：接线与否属行为决策，待产品确认
        systemPrompt = `你是 C4 Agent。\n\n${serviceCatalog}`;
    }

    // ── Step 4: Setup MCP manager（Unix socket 客户端；服务清单启动时确立）──
    const sockDir = resolveSockDir();
    const mcpManager = new C4McpManager(registry, sockDir, logger);
    logger.info(`MCP manager 已配置（socket 目录: ${sockDir}，从不拉起 MCP 进程）`);

    // ── Step 4.5: 对点核验显示服务（agent.md §3.6）──
    const displayService = new DisplayService({
        manager: mcpManager,
        configPath: config.shm_manager.config_path,
        staleThresholdMs: config.display?.stale_threshold_ms,
        logger,
    });
    const displayRouter = createDisplayRouter({ manager: displayService });
    const displayTools = createDisplayTools({ manager: displayService });

    // ── Step 5: Build model ──
    let model;
    try {
        model = await createModel(config, logger);
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`FATAL: 无法创建模型: ${msg}`);
        process.exit(1);
    }

    // ── Step 6: Create Agent State Tracker ──
    const stateTracker = new AgentStateTracker();

    // ── Step 7: Create C4 Agent ──
    let agent: C4Agent;
    try {
        agent = await createC4Agent({
            model,
            registry,
            mcpManager,
            configPath: config.shm_manager.config_path,
            agentConfigPath: configPath,
            instanceId: config.instance_id,
            site: config.site ?? null,
            state: stateTracker,
            agentLogger,
            displayTools,
        });
        logger.info("SuperWorker Agent 已创建");
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`FATAL: 无法创建 SuperWorker: ${msg}`);
        process.exit(1);
    }

    // ── Step 8: Create and start Express server ──
    const app = createApp({
        agent,
        stateProvider: stateTracker,
        corsOrigin: config.server.cors_origin,
        displayRouter,
        frontendDir: config.frontend?.dir,
        // MCP 存活状态＝连接状态推导（c4_architecture.md §3.1.1，C4_RS_00060/00068）
        aliveProvider: () => mcpManager.aliveStates(),
    });

    const { host, port } = config.server;
    app.listen(port, host, () => {
        logger.info(
            `Express 服务器已启动: http://${host}:${port}`,
        );
    });

    // ── Step 9: 重连收敛接线（§3.1.1：重连成功后按 §3.1.2 恢复流程收敛）──
    mcpManager.setReconnectHandler((serviceType: string) => {
        if (serviceType === SHM_SERVICE_TYPE) {
            // shm_manager 重连：对账 shm（幂等 create-or-attach）
            const shmClient = new ShmManagerClientAdapter(
                mcpManager,
                config.instance_id,
                config.shm_manager.config_path,
            );
            shmClient
                .create_shm()
                .then((r) =>
                    logger.info(`重连收敛: ${serviceType} create_shm → ${r}`),
                )
                .catch((err: unknown) => {
                    const msg = err instanceof Error ? err.message : String(err);
                    logger.warn(`重连收敛: ${serviceType} create_shm 失败: ${msg}`);
                });
            return;
        }
        // 数据服务重连：按当前 config 收敛（start 契约返回；ALREADY_RUNNING 无动作）
        void (async () => {
            const cfg = await load_valid_config(config.shm_manager.config_path);
            if (cfg === null) {
                return;
            }
            if (!data_service_types(cfg).includes(serviceType)) {
                return; // 无该服务配置段 = 期望零实例，无需动作
            }
            const client = new McpServiceClientAdapter(
                mcpManager,
                serviceType,
                config.instance_id,
                config.shm_manager.config_path,
            );
            const r = await client.start();
            logger.info(
                `重连收敛: ${serviceType} start → ${is_success_result(r) ? "success" : r}`,
            );
        })().catch((err: unknown) => {
            const msg = err instanceof Error ? err.message : String(err);
            logger.warn(`重连收敛: ${serviceType} 失败: ${msg}`);
        });
    });

    // ── Step 10: 四级瀑布启动恢复（持配置事务单飞锁直至收敛完成）──
    // 在服务器监听之后执行——收敛期间 Agent 仍可响应（C4_RS_00241）
    await runStartupWaterfall(config, mcpManager, registry, logger, stateTracker);

    // ── Step 11: Load / rebuild abbr registry ──
    // 记忆库是可重建派生数据：丢失/损坏/entries 为空时从 config.json 重建（agent.md §3.2.1.3a）
    const abbr_registry_path = path.join(
        path.dirname(config.shm_manager.config_path),
        "abbr_registry.json",
    );
    try {
        let data_config: SystemConfig | undefined;
        if (existsSync(config.shm_manager.config_path)) {
            try {
                const raw = await readFile(config.shm_manager.config_path, "utf-8");
                data_config = JSON.parse(raw) as SystemConfig;
            } catch {
                data_config = undefined;
            }
        }
        const abbr = await load_abbr_registry(
            abbr_registry_path,
            data_config,
            config.site ?? null,
        );
        if (abbr.entries.length > 0 || existsSync(abbr_registry_path)) {
            await save_abbr_registry(abbr, abbr_registry_path);
        }
        logger.info(`abbr 记忆库已加载（entries: ${abbr.entries.length}）`);
    } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`abbr 记忆库加载失败: ${msg}`);
    }

    logger.info("C4 Agent 就绪");

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
        agentLogger.close();
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
