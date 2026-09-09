// c4/agent/src/display/shm_client.ts — c4_shm_manager read_points 调用封装
// 设计：agent.md §3.6.2（数据读取通道）——Agent 不直接读 shm，
// 所有读取经 c4_shm_manager 的 read_points MCP 工具（确定性调用，不经 LLM）。

import type { MultiServerMCPClient } from "@langchain/mcp-adapters";
import { callToolViaMultiClient } from "../executor/executor.js";

const SHM_SERVER_NAME = "shm";

/** read_points 单点读取结果（c4_shm_manager.md §3.3 契约） */
export interface ReadEntry {
    shm_id: number;
    status: "ok" | "no_data";
    data_type?: number;
    timestamp_ms?: number;
    seq?: number;
    value?: number;
    /** value 字段 8 字节的 uint64 十进制串——权威位型（INT64/UINT64 ≥ 2^53 时以此为准） */
    value_raw?: string;
}

export interface ReadErrorEntry {
    shm_id: number;
    status: string;
}

export interface ReadPointsResult {
    reads: ReadEntry[];
    errors: ReadErrorEntry[];
}

/**
 * 批量读取数据块。单次调用覆盖全部请求点（工具上限 1000）。
 * 抛出异常 = 调用级失败（bridge 未连接 / c4_shm_manager 不可用 / 传输错误）；
 * 单点 contention 不抛异常，在返回值的 errors 中。
 */
export async function readPoints(
    multiClient: MultiServerMCPClient,
    shmIds: number[],
): Promise<ReadPointsResult> {
    if (shmIds.length === 0) {
        return { reads: [], errors: [] };
    }
    const text = await callToolViaMultiClient(
        multiClient,
        SHM_SERVER_NAME,
        "read_points",
        { shm_ids: shmIds },
    );
    let parsed: ReadPointsResult;
    try {
        parsed = JSON.parse(text) as ReadPointsResult;
    } catch {
        throw new Error(`read_points 返回非 JSON: ${text.slice(0, 200)}`);
    }
    if (!Array.isArray(parsed.reads) || !Array.isArray(parsed.errors)) {
        throw new Error(`read_points 返回结构异常: ${text.slice(0, 200)}`);
    }
    return parsed;
}
