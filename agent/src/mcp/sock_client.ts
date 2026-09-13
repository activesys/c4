// c4/agent/src/mcp/sock_client.ts — Unix-socket MCP 客户端传输层
// 设计：c4_architecture.md §3.1.1 —— MCP 服务为独立系统服务，监听
// <sock-dir>/<service>.sock；JSON-RPC 2.0 over Unix 流式 socket，
// 每连接一个 MCP 会话（initialize 握手按连接进行）。
// Agent 只连接、从不拉起 MCP 进程；断线自动重连（指数退避），断连即降级标记。

import * as net from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";

// ── UnixSocketTransport ───────────────────────────────────

/**
 * newline-delimited JSON-RPC 2.0 over Unix 流式 socket。
 * 帧协议与 Go MCP 服务的 resident 模式一致（每条消息一行 JSON + '\n'）。
 */
export class UnixSocketTransport implements Transport {
    onclose?: () => void;
    onerror?: (error: Error) => void;
    onmessage?: <T extends JSONRPCMessage>(message: T) => void;

    private readonly _sockPath: string;
    private _socket: net.Socket | null = null;
    private _buffer = "";
    private _closed = false;

    constructor(sockPath: string) {
        this._sockPath = sockPath;
    }

    start(): Promise<void> {
        return new Promise<void>((resolve, reject) => {
            const socket = net.createConnection(this._sockPath);
            let settled = false;

            socket.on("connect", () => {
                if (settled) {
                    return;
                }
                settled = true;
                this._socket = socket;
                resolve();
            });

            socket.on("error", (err: Error) => {
                this._handleError(err);
                if (!settled) {
                    settled = true;
                    reject(err);
                }
            });

            socket.on("data", (chunk: Buffer) => {
                this._buffer += chunk.toString("utf-8");
                let idx = this._buffer.indexOf("\n");
                while (idx >= 0) {
                    const line = this._buffer.slice(0, idx).trim();
                    this._buffer = this._buffer.slice(idx + 1);
                    if (line.length > 0) {
                        this._dispatch(line);
                    }
                    idx = this._buffer.indexOf("\n");
                }
            });

            socket.on("close", () => {
                this._socket = null;
                if (!this._closed) {
                    this._closed = true;
                    this.onclose?.();
                }
            });
        });
    }

    async send(message: JSONRPCMessage): Promise<void> {
        const socket = this._socket;
        if (!socket) {
            throw new Error(`transport closed (${this._sockPath})`);
        }
        const line = JSON.stringify(message) + "\n";
        await new Promise<void>((resolve, reject) => {
            socket.write(line, (err) => (err ? reject(err) : resolve()));
        });
    }

    async close(): Promise<void> {
        this._closed = true;
        const socket = this._socket;
        this._socket = null;
        if (socket) {
            socket.destroy();
        }
        this.onclose?.();
    }

    private _dispatch(line: string): void {
        let msg: JSONRPCMessage;
        try {
            msg = JSON.parse(line) as JSONRPCMessage;
        } catch (err: unknown) {
            this.onerror?.(
                err instanceof Error ? err : new Error(String(err)),
            );
            return;
        }
        this.onmessage?.(msg);
    }

    private _handleError(err: Error): void {
        // 传输层异常（含连接拒绝）——连接关闭时 onclose 负责状态迁移
        this.onerror?.(err);
    }
}

// ── 连接状态 ──────────────────────────────────────────────

export type LinkState = "disconnected" | "connecting" | "connected";

// ── ServiceConnection ─────────────────────────────────────

export interface ServiceConnectionOptions {
    connectTimeoutMs?: number;
    /** 重连初始退避（毫秒），指数递增，上限 maxBackoffMs */
    initialBackoffMs?: number;
    maxBackoffMs?: number;
    logger?: {
        info(msg: string): void;
        warn(msg: string): void;
        error(msg: string): void;
    };
    /** 重连成功后的收敛回调（§3.1.1：重连成功后按恢复流程收敛） */
    onReconnected?: (serviceType: string) => void;
}

const DEFAULT_CONNECT_TIMEOUT_MS = 5000;
const DEFAULT_INITIAL_BACKOFF_MS = 500;
const DEFAULT_MAX_BACKOFF_MS = 15000;

/**
 * 单个 MCP 服务的 Unix-socket 连接管理器。
 *
 * - connect(): 建立连接 + MCP initialize 握手 + listTools
 * - 断线 → 标记降级、指数退避后台重连（不阻塞其他服务）
 * - callToolText(): 工具调用（错误应答返回错误文本，不抛异常——与错误码分类兼容）
 */
export class ServiceConnection {
    readonly serviceType: string;
    private readonly _sockPath: string;
    private readonly _connectTimeoutMs: number;
    private readonly _initialBackoffMs: number;
    private readonly _maxBackoffMs: number;
    private readonly _logger?: ServiceConnectionOptions["logger"];
    private _onReconnected: ((serviceType: string) => void) | undefined;

    private _client: Client | null = null;
    private _transport: UnixSocketTransport | null = null;
    private _state: LinkState = "disconnected";
    private _degraded = false;
    private _backoffMs: number;
    private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    private _closed = false;
    private _connectSeq = 0;

    constructor(serviceType: string, sockPath: string, options: ServiceConnectionOptions = {}) {
        this.serviceType = serviceType;
        this._sockPath = sockPath;
        this._connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
        this._initialBackoffMs = options.initialBackoffMs ?? DEFAULT_INITIAL_BACKOFF_MS;
        this._maxBackoffMs = options.maxBackoffMs ?? DEFAULT_MAX_BACKOFF_MS;
        this._logger = options.logger;
        this._onReconnected = options.onReconnected;
        this._backoffMs = this._initialBackoffMs;
    }

    /** 设置重连成功后的收敛回调（§3.1.1：重连成功后按恢复流程收敛） */
    setReconnectedCallback(handler: (serviceType: string) => void): void {
        this._onReconnected = handler;
    }

    get state(): LinkState {
        return this._state;
    }

    get connected(): boolean {
        return this._state === "connected" && this._client !== null;
    }

    /** 降级标记：发生过断连或连接失败（§3.1.1 存活状态 = 连接状态推导） */
    get degraded(): boolean {
        return this._degraded;
    }

    /** 建立连接（含 initialize 握手）。失败时抛出；调用方可用 markDegraded 降级。 */
    async connect(): Promise<void> {
        if (this._closed) {
            throw new Error(`connection to ${this.serviceType} already closed`);
        }
        if (this.connected) {
            return;
        }
        this._cancelReconnectTimer();
        this._state = "connecting";
        const seq = ++this._connectSeq;

        const transport = new UnixSocketTransport(this._sockPath);
        const client = new Client(
            { name: "c4-agent", version: "1.0.0" },
            { capabilities: {} },
        );

        try {
            await this._withTimeout(
                client.connect(transport),
                this._connectTimeoutMs,
                `connect ${this.serviceType} timed out after ${this._connectTimeoutMs}ms`,
            );
        } catch (err: unknown) {
            void seq;
            try {
                await client.close();
            } catch {
                // 清理失败忽略
            }
            this._state = "disconnected";
            throw err;
        }

        transport.onclose = () => {
            if (this._transport === transport) {
                this._handleDisconnect();
            }
        };
        transport.onerror = (err: Error) => {
            this._logger?.warn(`${this.serviceType}: transport error: ${err.message}`);
        };

        this._client = client;
        this._transport = transport;
        this._state = "connected";
        this._backoffMs = this._initialBackoffMs;
        this._degraded = false;
        this._logger?.info(`${this.serviceType}: 已连接 (${this._sockPath})`);
    }

    /** 标记降级并安排后台重连（L1 非阻塞路径：不可连的服务退避重试，不阻塞其余服务） */
    markDegradedAndScheduleReconnect(reason: string): void {
        this._degraded = true;
        this._state = "disconnected";
        this._logger?.warn(
            `${this.serviceType}: 连接失败（${reason}），标记降级并退避重连`,
        );
        this._scheduleReconnect();
    }

    /**
     * 工具调用：返回应答文本。
     * MCP isError 应答返回其错误文本（如 "DUPLICATE_KEY: ..."），不抛异常；
     * 传输级失败（未连接/断连）抛出异常。
     */
    async callToolText(
        toolName: string,
        args: Record<string, unknown>,
        timeoutMs = 60000,
    ): Promise<string> {
        const client = this._client;
        if (!this.connected || !client) {
            throw new Error(
                `${this.serviceType} 未连接（socket: ${this._sockPath}）`,
            );
        }
        const result = (await this._withTimeout(
            client.callTool({ name: toolName, arguments: args }),
            timeoutMs,
            `${this.serviceType}.${toolName} timed out after ${timeoutMs}ms`,
        )) as { content?: Array<{ type: string; text?: string }>; isError?: boolean };

        const parts: string[] = [];
        if (Array.isArray(result?.content)) {
            for (const item of result.content) {
                if (item.type === "text" && typeof item.text === "string") {
                    parts.push(item.text);
                }
            }
        }
        const text = parts.length > 0 ? parts.join("\n") : JSON.stringify(result ?? {});
        return text;
    }

    /** 等待连接建立（L1 硬前置挂起点：c4_shm_manager 不可连时挂起收敛并退避等待） */
    async waitConnected(timeoutMs: number, pollMs = 200): Promise<boolean> {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            if (this.connected) {
                return true;
            }
            // 连接未建立且无重连排程时补一次（防止窗口期丢拍）
            if (this._state === "disconnected" && this._reconnectTimer === null && !this._closed) {
                this._scheduleReconnect(0);
            }
            await sleep(pollMs);
        }
        return this.connected;
    }

    async close(): Promise<void> {
        this._closed = true;
        this._cancelReconnectTimer();
        const client = this._client;
        this._client = null;
        this._transport = null;
        this._state = "disconnected";
        if (client) {
            try {
                await client.close();
            } catch {
                // 忽略清理错误
            }
        }
    }

    // ── 内部 ──

    private _handleDisconnect(): void {
        if (this._closed) {
            return;
        }
        this._client = null;
        this._transport = null;
        this._state = "disconnected";
        this._degraded = true;
        this._logger?.warn(`${this.serviceType}: 连接断开，退避重连中`);
        this._scheduleReconnect();
    }

    private _scheduleReconnect(delayMs?: number): void {
        if (this._closed) {
            return;
        }
        this._cancelReconnectTimer();
        const delay = delayMs ?? this._backoffMs;
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            void this._reconnectAttempt();
        }, delay);
        if (delayMs === undefined) {
            this._backoffMs = Math.min(this._backoffMs * 2, this._maxBackoffMs);
        }
    }

    private async _reconnectAttempt(): Promise<void> {
        if (this._closed || this.connected) {
            return;
        }
        try {
            await this.connect();
        } catch {
            if (!this._closed) {
                this._degraded = true;
                this._scheduleReconnect();
            }
            return;
        }
        // 重连成功 → 触发收敛回调（§3.1.1：重连成功后按恢复流程收敛）
        if (this._onReconnected) {
            try {
                this._onReconnected(this.serviceType);
            } catch (err: unknown) {
                const msg = err instanceof Error ? err.message : String(err);
                this._logger?.warn(`${this.serviceType}: 重连后收敛失败: ${msg}`);
            }
        }
    }

    private _cancelReconnectTimer(): void {
        if (this._reconnectTimer !== null) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = null;
        }
    }

    private async _withTimeout<T>(
        promise: Promise<T>,
        timeoutMs: number,
        errorMessage: string,
    ): Promise<T> {
        let timer: ReturnType<typeof setTimeout> | null = null;
        const timeout = new Promise<never>((_resolve, reject) => {
            timer = setTimeout(() => reject(new Error(errorMessage)), timeoutMs);
        });
        try {
            return await Promise.race([promise, timeout]);
        } finally {
            if (timer) {
                clearTimeout(timer);
            }
        }
    }
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}
