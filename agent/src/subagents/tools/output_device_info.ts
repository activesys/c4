// c4/agent/src/subagents/tools/output_device_info.ts — info-gatherer 结构化输出

import { tool } from "langchain";
import { z } from "zod";
import type { McpServiceRegistry } from "../../registry/registry.js";
import { find_service_type, list_supported_protocols, normalize_protocol } from "./output_plan_steps.js";

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
        protocol: z.string().describe("转发协议，必须由用户明确提供，禁止沿用接收侧协议或猜测"),
        missing_fields: z.array(z.string()).optional().describe("缺失的字段"),
    }).passthrough()).describe("实例 plan 字段（ip/port、url/token 等）+ 目标级字段（measurement）平铺"),
});

export function createOutputDeviceInfoTool(
    registry: McpServiceRegistry,
    config?: {
        declared_protocol?: string | null;
        declared_forward_protocol?: string | null;
        forward_handshake_pending?: boolean;
    },
) {
    return tool(
        async (input: z.infer<typeof deviceInfoSchema>) => {
            if (!input.devices || input.devices.length === 0) {
                return JSON.stringify({ success: false, error: "devices 不能为空" });
            }

            // 转发协议逐侧必供闸门（agent.md §3.2 协议）：存在转发目标时用户必须已显式
            // 声明转发协议——接收侧协议声明不算数，禁止沿用接收协议或按目标描述推断。
            // 拒绝即进入问答握手（pending）：super_worker 依据用户对下一轮提问的答复
            // 完成声明，任意措辞可收敛，不会死锁
            const declaredFwd = config?.declared_forward_protocol;
            const fwd = input.forward_targets ?? [];
            if (fwd.length > 0) {
                if (!declaredFwd) {
                    if (config) {
                        config.forward_handshake_pending = true;
                    }
                    return JSON.stringify({
                        success: false,
                        error:
                            "检测到转发目标，但用户尚未明确提供转发协议。" +
                            "请先向用户确认转发协议（接收侧协议不代表转发协议，禁止按目标描述猜测）；" +
                            "建议复述候选协议并以是非题提问（如「转发协议是 ASFP2 吗？」），" +
                            "用户答复确认后再重新调用本工具，不要在用户答复前反复重试。",
                    });
                }
                for (const ft of fwd) {
                    if (normalize_protocol(ft.protocol) !== declaredFwd) {
                        return JSON.stringify({
                            success: false,
                            error:
                                `转发目标 "${ft.name}" 的协议应为用户声明的 "${declaredFwd}"，` +
                                `而当前为 "${ft.protocol}"。请按用户声明的转发协议修正后重新调用，` +
                                `禁止改用其他协议`,
                        });
                    }
                }
            }

            // 协议声明校验：用户已在对话中明确声明协议时，工具调用不得使用其他协议
            //（防止重新解析时重新推断导致反复询问错误协议的业务字段，func_test_case 用例 11）
            const declared = config?.declared_protocol;
            if (declared) {
                for (const dev of input.devices) {
                    if (normalize_protocol(dev.protocol) !== declared) {
                        return JSON.stringify({
                            success: false,
                            error:
                                `设备 "${dev.name}" 的协议应为用户声明的 "${declared}"，` +
                                `而当前为 "${dev.protocol}"。请按用户声明的协议修正后重新调用，禁止重新推断`,
                        });
                    }
                }
            }

            // 点表完整性检查（协议已定时）：采集点的业务字段必须逐点完整保留，
            // 缺失在此处即报错，避免遗漏流入后续校验导致 LLM 盲目重试
            for (const dev of input.devices) {
                const svc_type = find_service_type(registry, normalize_protocol(dev.protocol), "writer");
                if (!svc_type) {
                    const supported = list_supported_protocols(registry, "writer").join("、");
                    return JSON.stringify({
                        success: false,
                        error:
                            `未在服务目录中找到协议 "${dev.protocol}"（设备 "${dev.name}"）。` +
                            `当前已部署的数据采集协议：${supported}。` +
                            `请确认协议名称是否正确，或确认该协议对应的 MCP 服务是否已部署` +
                            `（新协议可插拔接入，协议未部署前无法生成方案）。`,
                    });
                }
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
                "name 从对话上下文提取；abbr 为采集目标标识（候选，从描述提取）；" +
                "protocol 必须由用户提供（消息中的协议名或协议描述），禁止推断或猜测；" +
                "forward_targets 仅在用户已明确提供转发协议时才可提交（接收侧协议不算转发协议），" +
                "否则本工具会拒绝并要求先向用户询问。" +
                "实例 plan 字段（ip/port、url/token 等）直接平铺在 device 上，缺失时在 missing_fields 中列出。" +
                "协议可判定时，采集点必须逐点完整保留用户点表中的业务字段（如 addr）。",
            schema: deviceInfoSchema,
        },
    );
}
