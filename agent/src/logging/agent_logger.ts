// c4/agent/src/logging/agent_logger.ts — Agent 运行日志（结构化 NDJSON → 日志目录）
// 第二层日志：记录 Agent 运行期的完整交互流程，供事后调查追溯。
//   - 用户输入、LLM 调用与输出、工具调用与结果、记忆数据（abbr 记忆库 / config.json）
//   - 按天分文件 agent-YYYY-MM-DD.log（天然轮转），每行一个 JSON 对象（NDJSON）
// 第一层系统日志（启动/配置/异常）仍走 console → journald（见 index.ts 的 Logger）。

import * as fs from "node:fs";
import * as path from "node:path";

// ── 日志级别 ────────────────────────────────────────────────

export type AgentLogLevel = "debug" | "info" | "warn" | "error";

// ── AgentLogger ─────────────────────────────────────────────

/**
 * Agent 运行日志写入器。
 *
 * 将结构化事件写入日志目录（由 agent.json 的 logging.dir 指定，默认 /var/log/c4/agent），
 * 按天分文件。单进程单实例，串行 append，无需加锁。
 *
 * 所有写入方法均静默降级：日志目录不可写、文件创建失败等异常不会影响主流程。
 */
export class AgentLogger {
    private _dir: string;
    private _level: AgentLogLevel;
    private _stream: fs.WriteStream | null = null;
    private _streamDate: string = "";

    constructor(dir: string, level: AgentLogLevel = "debug") {
        this._dir = dir;
        this._level = level;
    }

    private _shouldLog(level: AgentLogLevel): boolean {
        const levels: AgentLogLevel[] = ["debug", "info", "warn", "error"];
        return levels.indexOf(level) >= levels.indexOf(this._level);
    }

    /** 本地日期 YYYY-MM-DD（工业服务器本地时区）。 */
    private _today(): string {
        const d = new Date();
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, "0");
        const day = String(d.getDate()).padStart(2, "0");
        return `${y}-${m}-${day}`;
    }

    /** 确保当前日期对应的文件流已打开（跨天自动切换）。 */
    private _ensureStream(): fs.WriteStream | null {
        const today = this._today();
        if (this._stream && this._streamDate === today) {
            return this._stream;
        }
        // 日期切换：关闭旧流，打开新文件
        this.close();
        try {
            fs.mkdirSync(this._dir, { recursive: true });
            this._streamDate = today;
            this._stream = fs.createWriteStream(
                path.join(this._dir, `agent-${today}.log`),
                { flags: "a" },
            );
        } catch (err: unknown) {
            this._stream = null;
            this._streamDate = "";
            console.error(
                `[ERROR] AgentLogger: 无法创建日志文件: ` +
                `${err instanceof Error ? err.message : String(err)}`,
            );
            return null;
        }
        return this._stream;
    }

    private _write(
        event: string,
        level: AgentLogLevel,
        conversation: string,
        data: Record<string, unknown>,
    ): void {
        try {
            if (!this._shouldLog(level)) return;
            const stream = this._ensureStream();
            if (!stream) return;
            const line = {
                ts: new Date().toISOString(),
                level,
                event,
                conversation,
                data,
            };
            stream.write(JSON.stringify(line) + "\n");
        } catch {
            // 日志写入失败不影响主流程
        }
    }

    // ── 类型化事件方法 ──────────────────────────────────────

    /** 用户输入（本轮最后一条消息）。 */
    user_input(conversation: string, role: string, content: string): void {
        this._write("user_input", "info", conversation, { role, content });
    }

    /** 送入 LLM 的消息（含注入的确认指令，按轮记录）。 */
    llm_call(
        conversation: string,
        round: number,
        messages: Array<{ role: string; content: string }>,
    ): void {
        this._write("llm_call", "debug", conversation, {
            round,
            message_count: messages.length,
            messages,
        });
    }

    /** LLM 文本输出（按消息聚合，避免逐 token 刷屏）。 */
    llm_text(conversation: string, content: string): void {
        this._write("llm_text", "info", conversation, { content });
    }

    /** 工具调用（含真实参数）。 */
    tool_call(conversation: string, name: string, args: unknown): void {
        this._write("tool_call", "info", conversation, { name, args });
    }

    /** 工具调用结果。 */
    tool_result(conversation: string, name: string, result: unknown): void {
        this._write("tool_result", "info", conversation, { name, result });
    }

    /** 记忆数据（abbr 记忆库、config.json 合并、Stop-Start 等）。 */
    memory(
        conversation: string,
        action: string,
        data: Record<string, unknown>,
    ): void {
        this._write("memory", "info", conversation, { action, ...data });
    }

    /** 状态机迁移（idle/collecting/planning/confirmed/executing）。 */
    phase(conversation: string, phase: string): void {
        this._write("phase", "info", conversation, { phase });
    }

    /** 错误事件。 */
    error(conversation: string, message: string): void {
        this._write("error", "error", conversation, { message });
    }

    /** 本轮结束。 */
    done(conversation: string): void {
        this._write("done", "info", conversation, {});
    }

    /** 关闭文件流（优雅退出时调用）。 */
    close(): void {
        if (this._stream) {
            try {
                this._stream.end();
            } catch {
                // ignore
            }
            this._stream = null;
            this._streamDate = "";
        }
    }
}
