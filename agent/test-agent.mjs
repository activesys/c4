// Test: does createAgent (langchain) properly bind tools to deepseek?
import { ChatDeepSeek } from "@langchain/deepseek";
import { createAgent } from "langchain";
import { z } from "zod";
import { tool } from "@langchain/core/tools";
import { HumanMessage } from "@langchain/core/messages";

const csv_parser = tool(
    async ({ filePath }) => `Parsed CSV: device=风机, ip=192.168.1.1`,
    {
        name: "csv_parser",
        description: "Parse CSV point table file to extract device info",
        schema: z.object({ filePath: z.string() }),
    }
);

const model = new ChatDeepSeek({
    model: "deepseek-chat",
    temperature: 0,
    apiKey: process.env.DEEPSEEK_API_KEY,
});

const agent = createAgent({
    model,
    tools: [csv_parser],
    systemPrompt: "You are an industrial data agent. Use csv_parser tool to parse uploaded files. Always call tools when needed.",
});

console.log("Testing createAgent tool binding...");
const response = await agent.invoke({
    messages: [
        new HumanMessage("用户上传了文件: name=points.csv\n文件内容:\ndevice_name,ip,port\n风机,192.168.1.1,502\n\n请解析此文件"),
    ]
});

// eslint-disable-next-line @typescript-eslint/no-explicit-any
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const msgs = /** @type {any} */ (response).messages || [];
let hasToolCall = false;
for (const m of msgs) {
    if (m.tool_calls && m.tool_calls.length > 0) {
        hasToolCall = true;
        console.log("TOOL CALL DETECTED:", m.tool_calls.map(/** @type {any} */ tc => tc.name));
    }
}
console.log("Has tool calls:", hasToolCall);
console.log("PASS" + (hasToolCall ? "" : " — FAIL: createAgent doesn't bind tools for deepseek"));
process.exit(hasToolCall ? 0 : 1);
