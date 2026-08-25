// c4/agent/src/subagents/tools/output_device_info.ts — info-gatherer 结构化输出

import { tool } from "langchain";
import { z } from "zod";

const deviceInfoSchema = z.object({
    devices: z.array(z.object({
        name: z.string().describe("设备名称，从对话上下文提取"),
        abbr: z.string().describe("采集目标标识（候选，info-gatherer 从描述提取）"),
        protocol: z.string().describe("通信协议，如 modbus_tcp"),
        points: z.array(z.object({
            name: z.string().describe("数据点名称"),
        }).passthrough()).describe("点字段宽松，具体字段由 point_fields 决定"),
        missing_fields: z.array(z.string()).optional().describe("缺失的字段"),
    }).passthrough()).describe("实例 plan 字段（ip/port、url/token 等）直接平铺"),
    forward_targets: z.array(z.object({
        name: z.string().describe("转发目标名称"),
        abbr: z.string().describe("转发目标标识（候选，info-gatherer 从描述提取）"),
        protocol: z.string().describe("转发协议，如 iec104"),
        missing_fields: z.array(z.string()).optional().describe("缺失的字段"),
    }).passthrough()).describe("实例 plan 字段（ip/port、url/token 等）+ 目标级字段（measurement）平铺"),
});

export const outputDeviceInfoTool = tool(
    async (input: z.infer<typeof deviceInfoSchema>) => {
        if (!input.devices || input.devices.length === 0) {
            return JSON.stringify({ success: false, error: "devices 不能为空" });
        }
        return JSON.stringify({ success: true, devices: input.devices });
    },
    {
        name: "output_device_info",
        description:
            "输出 info-gatherer 阶段的结构化设备信息。在获得 parser 的 raw data 后调用。" +
            "name 从对话上下文提取；abbr 为采集目标标识（候选，从描述提取）；protocol 根据数据特征推断；" +
            "实例 plan 字段（ip/port、url/token 等）直接平铺在 device 上，缺失时在 missing_fields 中列出。",
        schema: deviceInfoSchema,
    },
);