// c4/agent/src/subagents/tools/output_access_plan.ts — plan-generator 结构化输出
// LLM 在拿到 deviceInfo 后，调用此工具输出结构化 AccessPlan

import { tool } from "langchain";
import { z } from "zod";

// ── Schema ─────────────────────────────────────────────────

const devicePointSchema = z.object({
    name: z.string().describe("数据点名称"),
    addr: z.number().describe("协议地址"),
    uid: z.number().optional().describe("Modbus: 单元标识符"),
    fun: z.number().optional().describe("Modbus: 功能码"),
    type: z.number().optional().describe("Modbus: 数据类型"),
    swap: z.number().optional().describe("Modbus: 字节交换"),
});

const deviceSpecSchema = z.object({
    name: z.string().describe("设备名称"),
    seq: z.number().describe("设备编号（从1开始）"),
    protocol: z.string().describe("通信协议，如 modbus_tcp, iec104"),
    connection: z.object({
        ip: z.string().describe("设备 IP"),
        port: z.number().describe("端口"),
    }),
    points: z.array(devicePointSchema).describe("数据点列表"),
});

const forwardTargetSchema = z.object({
    name: z.string().describe("转发目标名称，如 中心侧数据库"),
    protocol: z.string().describe("转发协议，如 asfp2, influxdb"),
    connection: z.object({
        ip: z.string().optional().describe("目标 IP（asfp2 等 TCP 协议用）"),
        port: z.number().optional().describe("目标端口（asfp2 等 TCP 协议用）"),
        url: z.string().optional().describe("InfluxDB 写入端点 URL（influxdb 用，如 http://172.16.109.12:8086）"),
        token: z.string().optional().describe("InfluxDB 认证 token（influxdb 用）"),
        org: z.string().optional().describe("InfluxDB 组织名（influxdb 用）"),
        bucket: z.string().optional().describe("InfluxDB bucket 名（influxdb 用）"),
    }),
    measurement: z.string().optional().describe("InfluxDB measurement 名（influxdb 用，所有数据点共用；缺省时用场站缩写）"),
});

const accessPlanArgSchema = z.object({
    site: z.object({
        name: z.string().describe("场站名称，如 华能阿拉善"),
        abbr: z.string().describe("场站缩写，用于生成 instance.id，如 hnals"),
    }),
    devices: z.array(deviceSpecSchema).describe("采集设备列表"),
    forward_targets: z.array(forwardTargetSchema).optional().describe("转发目标列表"),
});

// ── 业务校验 ───────────────────────────────────────────────

function validate_access_plan(plan: Record<string, unknown>): { valid: boolean; errors: string[] } {
    const errors: string[] = [];

    if (!plan.site || typeof plan.site !== "object") {
        errors.push("site: 必须提供场站信息（name 和 abbr）");
    } else {
        const site = plan.site as Record<string, unknown>;
        if (typeof site.name !== "string" || site.name.trim().length === 0)
            errors.push("site.name: 必须是非空字符串");
        if (typeof site.abbr !== "string" || site.abbr.trim().length === 0)
            errors.push("site.abbr: 必须是非空字符串");
        else if (!/^[a-zA-Z_]+$/.test(site.abbr))
            errors.push(`site.abbr: "${site.abbr}" 仅允许 [a-zA-Z_]+`);
    }

    if (!Array.isArray(plan.devices) || plan.devices.length === 0) {
        errors.push("devices: 必须是非空数组");
    } else {
        for (let i = 0; i < (plan.devices as unknown[]).length; i++) {
            const dev = (plan.devices as unknown[])[i] as Record<string, unknown>;
            const prefix = `devices[${i}]`;
            if (typeof dev.name !== "string" || dev.name.trim().length === 0)
                errors.push(`${prefix}.name: 必须是非空字符串`);
            if (typeof dev.seq !== "number" || dev.seq < 1)
                errors.push(`${prefix}.seq: 必须是正整数`);
            if (typeof dev.protocol !== "string" || dev.protocol.trim().length === 0)
                errors.push(`${prefix}.protocol: 必须是非空字符串`);
            if (!dev.connection || typeof dev.connection !== "object")
                errors.push(`${prefix}.connection: 必须提供连接信息`);
            if (!Array.isArray(dev.points) || dev.points.length === 0)
                errors.push(`${prefix}.points: 必须是非空数组`);
        }
    }

    return { valid: errors.length === 0, errors };
}

// ── 工具 ──────────────────────────────────────────────────

export const outputAccessPlanTool = tool(
    async (input: z.infer<typeof accessPlanArgSchema>) => {
        const vr = validate_access_plan(input as unknown as Record<string, unknown>);

        if (!vr.valid) {
            return JSON.stringify({
                success: false,
                errors: vr.errors,
                hint: "请根据 errors 修正后重试。site.abbr 仅允许 [a-zA-Z_]+。",
            });
        }

        return JSON.stringify({
            success: true,
            site: input.site,
            devices: input.devices,
            forward_targets: input.forward_targets || [],
            summary: {
                device_count: input.devices.length,
                total_points: input.devices.reduce((sum, d) => sum + d.points.length, 0),
                has_forward: (input.forward_targets || []).length > 0,
            },
        });
    },
    {
        name: "output_access_plan",
        description:
            "生成结构化接入方案 (AccessPlan)。" +
            "在获得设备信息后，结合 service_catalog 选择匹配的服务类型，" +
            "推断场站名称和缩写，组织设备清单和转发目标。" +
            "site.abbr 用于生成 instance.id，如 hnals_wt1（hnals=场站缩写, wt1=采集目标标识）。" +
            "调用时机：用户要求生成方案时。",
        schema: accessPlanArgSchema,
    },
);
