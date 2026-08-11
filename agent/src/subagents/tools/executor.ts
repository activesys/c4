// c4/agent/src/subagents/tools/executor.ts — execute_access_plan tool
// 将 step-decomposer 输出的 AccessPlanSteps 写入 config.json
// 根据 agent.md §3.2.1 实现

import { StructuredTool } from "@langchain/core/tools";
import { z } from "zod";
import { merge_config_from_steps } from "../../executor/executor.js";
import type { ServiceStep } from "../../types/index.js";
import type { RegistryLookup } from "../../executor/executor.js";

/**
 * execute_access_plan — 执行接入方案。
 *
 * 接收 step-decomposer 输出的 ServiceStep[]，
 * 调用 merge_config_from_steps 原子写入 config.json。
 */
export class ExecuteAccessPlanTool extends StructuredTool {
    name = "execute_access_plan";
    description =
        "将经用户确认的接入方案步骤写入 config.json，完成配置持久化。" +
        "参数 steps 为 step-decomposer 输出的操作步骤数组。" +
        "在用户明确确认方案后调用此工具完成执行。";

    schema = z.object({
        steps: z
            .array(z.any())
            .describe("step-decomposer 输出的 ServiceStep 数组"),
    });

    private config_path: string;
    private registry: RegistryLookup | undefined;

    constructor(config_path: string, registry?: RegistryLookup) {
        super();
        this.config_path = config_path;
        this.registry = registry;
    }

    async _call(
        input: z.infer<typeof this.schema>,
    ): Promise<string> {
        const { steps } = input;
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const typed_steps = steps as ServiceStep[];

        try {
            const result = await merge_config_from_steps(
                typed_steps,
                this.config_path,
                this.registry,
            );

            if (!result.success) {
                return JSON.stringify({
                    success: false,
                    error: `配置合并失败: ${result.error ?? "未知错误"}`,
                });
            }

            return JSON.stringify({
                success: true,
                message: `接入方案已执行。写入 ${typed_steps.length} 个步骤。` +
                    (result.warnings && result.warnings.length > 0
                        ? ` 警告: ${result.warnings.join("; ")}`
                        : ""),
            });
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : String(err);
            return JSON.stringify({
                success: false,
                error: `执行失败: ${msg}`,
            });
        }
    }
}
