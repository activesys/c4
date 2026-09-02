// c4/agent/src/subagents/tools/output_device_info.ts — info-gatherer 结构化输出

import { tool } from "langchain";
import { z } from "zod";
import type { McpServiceRegistry } from "../../registry/registry.js";
import { find_service_type, normalize_protocol } from "./output_plan_steps.js";

const deviceInfoSchema = z.object({
    devices: z.array(z.object({
        name: z.string().describe("设备名称，从对话上下文提取"),
        abbr: z.string().describe("采集目标标识（候选，info-gatherer 从描述提取）"),
        protocol: z.string().describe("通信协议，如 modbus"),
        points: z.array(z.object({
            name: z.string().describe("数据点名称（英文标识；无点名传空字符串，系统按身份字段自动生成）"),
        }).passthrough()).describe("点字段宽松，具体字段由 point_schema.fields 决定"),
        missing_fields: z.array(z.string()).optional().describe("缺失的字段"),
    }).passthrough()).describe("实例 plan 字段（ip/port、url/token 等）直接平铺"),
    forward_targets: z.array(z.object({
        name: z.string().describe("转发目标名称"),
        abbr: z.string().describe("转发目标标识（候选，info-gatherer 从描述提取）"),
        protocol: z.string().describe("转发协议，如 asfp2"),
        missing_fields: z.array(z.string()).optional().describe("缺失的字段"),
    }).passthrough()).describe("实例 plan 字段（ip/port、url/token 等）+ 目标级字段（measurement）平铺"),
});

export function createOutputDeviceInfoTool(registry: McpServiceRegistry) {
    return tool(
        async (input: z.infer<typeof deviceInfoSchema>) => {
            if (!input.devices || input.devices.length === 0) {
                return JSON.stringify({ success: false, error: "devices 不能为空" });
            }

            // 点表完整性检查（协议已定时）：采集点的业务字段必须逐点完整保留，
            // 缺失在此处即报错，避免遗漏流入后续校验导致 LLM 盲目重试
            for (const dev of input.devices) {
                const svc_type = find_service_type(registry, normalize_protocol(dev.protocol), "writer");
                if (!svc_type) continue;
                const entry = registry.queryRegistry(svc_type);
                if (!entry) continue;

                const missing_points: number[] = [];
                for (let i = 0; i < dev.points.length; i++) {
                    const pt = dev.points[i] as Record<string, unknown>;
                    const lacking = entry.point_schema.fields
                        .filter((f) => {
                            const v = pt[f.name];
                            return v === undefined || v === null || v === "";
                        })
                        .map((f) => f.name);
                    if (lacking.length > 0) {
                        missing_points.push(i + 1);
                    }
                }
                if (missing_points.length > 0) {
                    return JSON.stringify({
                        success: false,
                        error:
                            `设备 "${dev.name}" 共 ${dev.points.length} 个点中有 ${missing_points.length} 个` +
                            `（第 ${missing_points.join("、")} 个）缺少业务字段，` +
                            `请从用户消息或点表逐点补齐后重新调用，禁止编造`,
                    });
                }
            }

            return JSON.stringify({
                success: true,
                devices: input.devices,
                forward_targets: (input as Record<string, unknown>).forward_targets ?? [],
            });
        },
        {
            name: "output_device_info",
            description:
                "输出 info-gatherer 阶段的结构化设备信息。在获得 parser 的 raw data 后调用。" +
                "name 从对话上下文提取；abbr 为采集目标标识（候选，从描述提取）；protocol 根据数据特征推断；" +
                "实例 plan 字段（ip/port、url/token 等）直接平铺在 device 上，缺失时在 missing_fields 中列出。" +
                "协议可判定时，采集点必须逐点完整保留用户点表中的业务字段（如 addr）。",
            schema: deviceInfoSchema,
        },
    );
}
