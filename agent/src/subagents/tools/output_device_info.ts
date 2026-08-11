// c4/agent/src/subagents/tools/output_device_info.ts — doc-parser 结构化输出

import { tool } from "langchain";
import { z } from "zod";

const deviceInfoSchema = z.object({
    devices: z.array(z.object({
        name: z.string().describe("设备名称，从对话上下文提取"),
        protocol: z.string().describe("通信协议，如 modbus_tcp"),
        connection: z.object({
            ip: z.string().describe("设备 IP，缺失时填 ''"),
            port: z.number().describe("端口号"),
        }),
        points: z.array(z.object({
            name: z.string().describe("数据点名称"),
            addr: z.number().describe("协议地址"),
            uid: z.number().optional().describe("Modbus 单元标识符"),
            fun: z.number().optional().describe("Modbus 功能码"),
            type: z.number().optional().describe("Modbus 数据类型"),
            swap: z.number().optional().describe("Modbus 字节交换"),
        })),
        missing_fields: z.array(z.string()).optional().describe("缺失的字段"),
    })),
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
            "输出 doc-parser 阶段的结构化设备信息。在获得 parser 的 raw data 后调用。" +
            "name 从对话上下文提取；protocol 根据数据特征推断；" +
            "connection.ip 缺失时填 '' 并在 missing_fields 中列出。",
        schema: deviceInfoSchema,
    },
);
