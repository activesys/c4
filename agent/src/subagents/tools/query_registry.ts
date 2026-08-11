// c4/agent/src/subagents/tools/query_registry.ts — Registry 查询工具
// step-decomposer 按需拉取完整 RegistryEntry

import { tool } from "langchain";
import { z } from "zod";
import type { McpServiceRegistry } from "../../registry/registry.js";

export function createQueryRegistryTool(registry: McpServiceRegistry) {
    return tool(
        async ({ service_type }: { service_type: string }) => {
            if (!service_type || service_type.trim().length === 0) {
                return JSON.stringify({
                    success: false,
                    error: "service_type 不能为空",
                    available_services: registry.getServiceTypes(),
                });
            }
            const entry = registry.queryRegistry(service_type);
            if (!entry) {
                return JSON.stringify({
                    success: false,
                    error: `未找到服务类型 "${service_type}"`,
                    available_services: registry.getServiceTypes(),
                    hint: "请从 available_services 中选择正确的 service_type",
                });
            }
            return JSON.stringify({ success: true, entry });
        },
        {
            name: "query_registry",
            description: "查询指定 MCP 服务类型的完整注册信息（含 config_schema、binary_path）。" +
                "参数 service_type 如 \"c4_modbus_client\"。",
            schema: z.object({
                service_type: z.string().describe("MCP 服务类型标识符，如 c4_modbus_client"),
            }),
        },
    );
}
