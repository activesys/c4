// c4/agent/src/super_worker/turn_rules.ts
// 回合状态机强化纯函数（agent.md §2.4，v0.5.0）——阶段门禁 / 提问即终局。
// 独立于 LangChain 运行时，可被 super_worker 与单元测试直接引用。

// ── 阶段（与 server/types AgentPhase 对齐）──────────────────

export type Phase = "idle" | "collecting" | "planning" | "confirmed" | "executing";

/** 合法转移表（agent.md §2.4.1）：不在表内的转移一律拒绝并 LOG。 */
export const LEGAL_TRANSITIONS: Readonly<Record<Phase, readonly Phase[]>> = {
    idle: ["collecting"],
    collecting: ["idle", "collecting", "planning"],
    planning: ["idle", "collecting", "planning", "confirmed"],
    confirmed: ["idle", "collecting", "planning", "executing"],
    executing: ["idle", "collecting", "planning", "executing"],
};

export function can_transition(from: Phase, to: Phase): boolean {
    return (LEGAL_TRANSITIONS[from] ?? []).includes(to);
}

// ── 问询句式判定（agent.md §2.4.2）─────────────────────────

/** 方案确认句式——questionPending 排除清单（命中走按钮 arm 判定，不置位）。 */
export const CONFIRM_PHRASE_RE = /是否确认执行|确认执行/;

/**
 * 问询句式命中（回合终结时对每条 AI 消息的累积文本判定）：
 * 按句切分（。；！\n），句末以 ？/? 收尾，或句首为前缀词（请提供/请补充/请确认/请问/是否）
 * 即命中；方案确认句式优先排除。多问句轮次取保守语义：任一命中即置位。
 */
export function question_hit(text: string): boolean {
    if (CONFIRM_PHRASE_RE.test(text)) {
        return false;
    }
    for (const sent of (text ?? "").split(/[。；！\n]/)) {
        const s = sent.trim();
        if (!s) {
            continue;
        }
        if (s.endsWith("？") || s.endsWith("?")) {
            return true;
        }
        if (/^(请提供|请补充|请确认|请问|是否)/.test(s)) {
            return true;
        }
    }
    return false;
}

// ── 工具闸门（agent.md §2.4.1 工具前置条件表）────────────────

export interface GateSnapshot {
    phase: Phase;
    /** 本回合内 LLM 文本已命中问询句式（提问即终局） */
    questionPending: boolean;
    /** 本会话已有成功的 output_device_info（deviceInfo 非空，跨回合保持） */
    deviceInfoReady: boolean;
    /** 执行闸门：是否收到过确认按钮消息 */
    userConfirmed: boolean;
}

/** 需要闸门放行的工具名集合 */
export const GATED_TOOLS: ReadonlySet<string> = new Set([
    "output_device_info",
    "output_access_plan",
    "output_plan_steps",
]);

/**
 * 工具闸门判定：返回 null = 放行；返回字符串 = 可读拒绝理由（注入工具结果，
 * 引导 LLM 结束回合/补齐信息，不抛异常）。判定表即 agent.md §2.4.1：
 * - output_device_info：phase ≠ executing
 * - output_access_plan：会话级 deviceInfo 就绪 且 非提问挂起
 * - output_plan_steps：phase ∈ {confirmed, planning} 且 userConfirmed 且 非提问挂起
 *   （planning + userConfirmed 覆盖执行失败修复轮，agent.md §2.4.1 修复语义）
 */
export function check_tool_gate(tool: string, s: GateSnapshot): string | null {
    if (!GATED_TOOLS.has(tool)) {
        return null;
    }
    if (s.questionPending) {
        return "已向用户提出待回答的问题——本轮必须结束并等待用户答复，"
            + "禁止继续调用方案/执行类工具（agent.md §2.4.2 提问即终局）。";
    }
    switch (tool) {
        case "output_device_info":
            return s.phase === "executing"
                ? "执行进行中，不能重新收集设备信息；请先完成或终止当前执行。"
                : null;
        case "output_access_plan":
            if (!s.deviceInfoReady) {
                return "尚未完成设备信息整理（output_device_info）——请先收集并经用户确认必要信息，"
                    + "再生成接入方案（agent.md §2.4.1 阶段门禁）。";
            }
            return null;
        case "output_plan_steps":
            if (s.phase !== "confirmed" && !(s.phase === "planning" && s.userConfirmed)) {
                return "尚未收到确认按钮消息——请先展示接入方案并等待用户点击「确认」按钮"
                    + "（agent.md §2.4.1 阶段门禁）。";
            }
            return null;
        default:
            return null;
    }
}
