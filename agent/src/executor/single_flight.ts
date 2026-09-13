// c4/agent/src/executor/single_flight.ts — config.json 变更单飞互斥锁
// 设计：c4_architecture.md §3.1.2 单飞规则 —— config.json 变更是单飞操作：
// 进程级配置事务互斥锁自写入 pending_change.json 起持有，至删除事务标记释放；
// 并发配置变更请求在会话层直接拒绝，向用户提示固定话术；
// 启动恢复瀑布持同一把锁直至收敛完成。
// 每个 C4 实例仅一个 Agent 进程，且只有 Agent 写 config.json——
// 进程内异步互斥锁已足够。

/** 并发变更请求被拒绝时向用户展示的固定话术（§3.1.2） */
export const CONFIG_BUSY_MESSAGE = "有配置变更正在执行，请稍后重试";

/** 锁被占用时抛出——调用方（会话层）捕获后向用户展示 CONFIG_BUSY_MESSAGE */
export class ConfigBusyError extends Error {
    constructor() {
        super(CONFIG_BUSY_MESSAGE);
        this.name = "ConfigBusyError";
    }
}

let _locked = false;

export function is_config_locked(): boolean {
    return _locked;
}

/**
 * 以单飞方式执行 fn：锁空闲 → 持锁执行；锁被占 → 立即抛出 ConfigBusyError
 * （不排队——并发变更请求在会话层直接拒绝）。
 */
export async function with_config_lock<T>(fn: () => Promise<T>): Promise<T> {
    if (_locked) {
        throw new ConfigBusyError();
    }
    _locked = true;
    try {
        return await fn();
    } finally {
        _locked = false;
    }
}
