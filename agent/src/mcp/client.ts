// c4/agent/src/mcp/client.ts — MCP 客户端管理（独立服务模型）
// 设计：c4_architecture.md §3.1.1 —— MCP 服务是独立系统服务，监听
// <sock-dir>/<service>.sock；Agent 是 MCP 客户端，经 Unix socket 连接，
// 从不拉起 MCP 进程。服务清单在 Agent 启动时建立（重启 = 识别边界），
// 不支持运行期热发现。sock 目录：env C4_SOCK_DIR，默认 /run/c4。

import * as path from "node:path";
import type { McpServiceRegistry } from "../registry/registry.js";
import {
    ServiceConnection,
    type LinkState,
} from "./sock_client.js";

// ── 常量 ──────────────────────────────────────────────────

export const SHM_SERVICE_TYPE = "c4_shm_manager";

/** 生产 socket 目录（systemd RuntimeDirectory=c4）；测试经 C4_SOCK_DIR 覆盖 */
export const DEFAULT_SOCK_DIR = "/run/c4";

export function resolveSockDir(): string {
    const env = process.env["C4_SOCK_DIR"];
    return env && env.length > 0 ? env : DEFAULT_SOCK_DIR;
}

// ── 错误翻译（保留中文本地化）─────────────────────────────

const BUILTIN_ERRORS: Record<string, string> = {
    SHM_CORRUPTED: "数据存储异常，请联系管理员检查共享内存状态",
    SHM_SYSCALL_FAILED: "系统资源不足，共享内存操作失败",
    CONNECTION_REFUSED: "设备连接失败，请确认设备已开机且网络可达",
    TIMEOUT: "设备响应超时，请检查网络连接",
    INVALID_CONFIG: "配置参数有误，请检查提交的信息",
};

export function buildErrorTranslator(registry: McpServiceRegistry): Record<string, string> {
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

export function translateErrorText(message: string, translations: Record<string, string>): string {
    for (const [code, msg] of Object.entries(translations)) {
        if (message.includes(code)) return msg;
    }
    return message;
}

// ── C4McpManager ──────────────────────────────────────────

export interface ManagerLogger {
    info(msg: string): void;
    warn(msg: string): void;
    error(msg: string): void;
    debug?(msg: string): void;
}

/** 服务存活状态（连接状态推导，供 Web 页面展示与告警——C4_RS_00060/00068） */
export interface ServiceAliveState {
    service_type: string;
    alive: boolean;
    degraded: boolean;
}

/**
 * C4McpManager —— 全部 MCP 服务的连接管理。
 *
 * - 服务清单构造时确立（c4_shm_manager + Registry 全部条目），重启 Agent 才会重建
 * - connectAll(): 逐服务连 socket（互相不阻塞；失败 → 降级标记 + 后台退避重连）
 * - waitShmConnected(): L1 硬前置——c4_shm_manager 不可连时挂起数据服务收敛并退避等待
 * - callToolText(): 按服务类型路由工具调用
 */
export class C4McpManager {
    private readonly _connections = new Map<string, ServiceConnection>();
    private readonly _logger: ManagerLogger;
    private readonly _sockDir: string;

    constructor(registry: McpServiceRegistry, sockDir: string, logger: ManagerLogger) {
        this._sockDir = sockDir;
        this._logger = logger;

        // 服务清单启动时确立（重启 = 识别边界，无热发现——c4_architecture.md §3.1.1）
        const serviceTypes = new Set<string>([SHM_SERVICE_TYPE]);
        for (const entry of registry.getServiceCatalogEntries()) {
            serviceTypes.add(entry.service_type);
        }
        for (const svc of serviceTypes) {
            this._connections.set(
                svc,
                new ServiceConnection(svc, this.sockPath(svc), {
                    logger: {
                        info: (m) => this._logger.info(m),
                        warn: (m) => this._logger.warn(m),
                        error: (m) => this._logger.error(m),
                    },
                }),
            );
        }
    }

    sockPath(serviceType: string): string {
        return path.join(this._sockDir, `${serviceType}.sock`);
    }

    /** 设置重连成功后的收敛回调（§3.1.1：重连成功后按恢复流程收敛） */
    setReconnectHandler(handler: (serviceType: string) => void): void {
        for (const conn of this._connections.values()) {
            conn.setReconnectedCallback(handler);
        }
    }

    /**
     * 逐服务连接（互相不阻塞）。失败的服务标记降级并安排退避重连，
     * 不抛异常——L1 语义（C4_RS_00242）。
     */
    async connectAll(): Promise<void> {
        await Promise.all(
            [...this._connections.values()].map(async (conn) => {
                try {
                    await conn.connect();
                } catch (err: unknown) {
                    const msg = err instanceof Error ? err.message : String(err);
                    conn.markDegradedAndScheduleReconnect(msg);
                }
            }),
        );
    }

    /**
     * L1 硬前置：等待 c4_shm_manager 连接建立（挂起 + 退避等待）。
     * 超过 timeoutMs 仍不可连 → false（调用方 fail-fast 报告，不得以
     * SHM_OPEN_FAILED 告警风暴的形式失败）。
     */
    async waitShmConnected(timeoutMs: number): Promise<boolean> {
        const shm = this._connections.get(SHM_SERVICE_TYPE);
        if (!shm) {
            return false;
        }
        if (shm.connected) {
            return true;
        }
        // 首次尝试同步连接（测试栈中 shm_manager 通常已就绪）
        try {
            await shm.connect();
            return true;
        } catch {
            shm.markDegradedAndScheduleReconnect("shm socket 不可连（挂起数据服务收敛）");
        }
        return shm.waitConnected(timeoutMs, 500);
    }

    isConnected(serviceType: string): boolean {
        return this._connections.get(serviceType)?.connected ?? false;
    }

    isDegraded(serviceType: string): boolean {
        return this._connections.get(serviceType)?.degraded ?? false;
    }

    linkState(serviceType: string): LinkState {
        return this._connections.get(serviceType)?.state ?? "disconnected";
    }

    /** 全部服务的存活状态快照（连接状态推导） */
    aliveStates(): ServiceAliveState[] {
        return [...this._connections.values()].map((conn) => ({
            service_type: conn.serviceType,
            alive: conn.connected,
            degraded: conn.degraded,
        }));
    }

    serviceTypes(): string[] {
        return [...this._connections.keys()];
    }

    /**
     * 工具调用：返回应答文本（isError 应答返回错误文本，不抛异常）。
     * 服务未连接 → 抛出异常（调用方按降级处理）。
     */
    async callToolText(
        serviceType: string,
        toolName: string,
        args: Record<string, unknown>,
        timeoutMs?: number,
    ): Promise<string> {
        const conn = this._connections.get(serviceType);
        if (!conn) {
            throw new Error(`未知服务类型: ${serviceType}（不在启动时建立的服务清单中）`);
        }
        return conn.callToolText(toolName, args, timeoutMs);
    }

    async close(): Promise<void> {
        await Promise.all(
            [...this._connections.values()].map((conn) => conn.close()),
        );
    }
}
