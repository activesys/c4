// c4/agent/src/mcp/bridge.ts — MCP Client Bridge
// 根据 agent.md §3.4 实现
// 通过 @modelcontextprotocol/sdk 的 StdioClientTransport 管理 Go MCP 服务连接

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import type {
  Tool,
} from "@modelcontextprotocol/sdk/types.js";

// ── MCP 连接状态 ──
export type BridgeStatus = "disconnected" | "connecting" | "connected" | "error";

// ── 工具列表结果（简化）──
export interface McpToolList {
  tools: Tool[];
}

// ── 工具调用参数 ──
export interface ToolCallParams {
  name: string;
  arguments: Record<string, unknown>;
}

// ── 连接选项 ──
export interface BridgeOptions {
  connectTimeoutMs?: number;
  stderr?: "pipe" | "inherit";
  rootUris?: string[];
}

/** 默认连接超时: 30 秒 */
const DEFAULT_CONNECT_TIMEOUT_MS = 30000;

/**
 * McpClientBridge — 单个 MCP 服务的 stdio 连接管理器。
 *
 * 每个实例管理一个 Go MCP 服务的完整生命周期：
 * 1. spawn 子进程 (binary_path + args)
 * 2. MCP initialize 握手
 * 3. list_tools / call_tool
 * 4. disconnect 关闭
 */
export class McpClientBridge {
  private readonly _binaryPath: string;
  private readonly _args: string[];
  private readonly _connectTimeoutMs: number;
  private readonly _stderrMode: "pipe" | "inherit";

  private _client: Client | null = null;
  private _transport: StdioClientTransport | null = null;
  private _status: BridgeStatus = "disconnected";
  private _tools: Tool[] = [];
  private _errorMessage: string | null = null;
  private readonly _rootUris: string[];

  /**
   * @param binaryPath - MCP 服务二进制路径
   * @param args - 命令行参数
   * @param options - 连接选项
   */
  constructor(
    binaryPath: string,
    args: string[] = [],
    options: BridgeOptions = {}
  ) {
    this._binaryPath = binaryPath;
    this._args = args;
    this._connectTimeoutMs =
      options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this._stderrMode = options.stderr ?? "inherit";
    this._rootUris = options.rootUris ?? [];
  }

  // ── 属性 ──

  get status(): BridgeStatus {
    return this._status;
  }

  get isConnected(): boolean {
    return this._status === "connected";
  }

  get tools(): ReadonlyArray<Tool> {
    return this._tools;
  }

  get errorMessage(): string | null {
    return this._errorMessage;
  }

  /**
   * 建立 MCP 连接: spawn 进程 → initialize 握手 → list_tools。
   *
   * @throws 连接或握手失败时抛出
   */
  async connect(): Promise<void> {
    if (this._status === "connected") {
      return;
    }

    this._status = "connecting";
    this._errorMessage = null;

    try {
      // 创建 stdio transport
      this._transport = new StdioClientTransport({
        command: this._binaryPath,
        args: this._args,
        stderr: this._stderrMode,
      });

      // 创建 MCP client
      this._client = new Client(
        { name: "c4-agent", version: "1.0.0" },
        { capabilities: {} }
      );

      // 连接带超时
      await this._withTimeout(
        this._client.connect(this._transport),
        this._connectTimeoutMs,
        `MCP connect timed out after ${this._connectTimeoutMs}ms`
      );

      // 获取工具列表
      const result = await this._withTimeout(
        this._client.listTools(),
        this._connectTimeoutMs,
        "MCP listTools timed out"
      );

      this._tools = result.tools;
      this._status = "connected";
    } catch (err: unknown) {
      this._status = "error";
      this._errorMessage = err instanceof Error ? err.message : String(err);
      // 清理失败的连接
      await this._cleanup();
      throw err;
    }
  }

  /**
   * 断开 MCP 连接，关闭子进程。
   */
  async disconnect(): Promise<void> {
    if (this._status === "disconnected") {
      return;
    }

    await this._cleanup();
    this._status = "disconnected";
    this._tools = [];
    this._errorMessage = null;
  }

  /**
   * 列出 MCP 服务提供的工具。
   *
   * @returns 工具列表
   * @throws 若未连接
   */
  async listTools(): Promise<Tool[]> {
    this._assertConnected();
    const result = await this._client!.listTools();
    this._tools = result.tools;
    return this._tools;
  }

  /**
   * 调用 MCP 工具。
   *
   * @param params - 工具调用参数
   * @returns MCP 调用结果
   * @throws 若未连接或调用失败
   */
  async callTool(params: ToolCallParams): Promise<unknown> {
    this._assertConnected();
    return this._client!.callTool({
      name: params.name,
      arguments: params.arguments,
    });
  }

  /**
   * 获取服务端信息（初始化握手后可用）。
   */
  getServerVersion(): { name: string; version: string } | null {
    if (!this._client) {
      return null;
    }
    const info = this._client.getServerVersion();
    return info
      ? { name: info.name, version: info.version }
      : null;
  }

  // ── 内部方法 ──

  /**
   * 断言已连接，否则抛出。
   */
  private _assertConnected(): void {
    if (this._status !== "connected" || !this._client || !this._transport) {
      throw new Error(
        `MCP Bridge not connected, current status: ${this._status}`
      );
    }
  }

  /**
   * 清理 transport 和 client。
   */
  private async _cleanup(): Promise<void> {
    try {
      if (this._client) {
        // Client.close() 会断开 transport
        await this._client.close();
      }
    } catch {
      // 忽略清理错误
    } finally {
      this._client = null;
      this._transport = null;
    }
  }

  /**
   * Promise 超时包装。
   */
  private async _withTimeout<T>(
    promise: Promise<T>,
    timeoutMs: number,
    errorMessage: string
  ): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | null = null;

    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        reject(new Error(errorMessage));
      }, timeoutMs);
    });

    try {
      const result = await Promise.race([promise, timeout]);
      return result;
    } finally {
      if (timer) {
        clearTimeout(timer);
      }
    }
  }
}

/**
 * 创建并连接 MCP 桥接的便捷工厂方法。
 *
 * @param binaryPath - MCP 服务二进制路径
 * @param args - 命令行参数
 * @param options - 连接选项
 * @returns 已连接的 McpClientBridge 实例
 */
export async function connectBridge(
  binaryPath: string,
  args: string[] = [],
  options: BridgeOptions = {}
): Promise<McpClientBridge> {
  const bridge = new McpClientBridge(binaryPath, args, options);
  await bridge.connect();
  return bridge;
}
