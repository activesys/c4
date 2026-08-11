// Quick test: verify ChatDeepSeek tool calling works
import { ChatDeepSeek } from "@langchain/deepseek";
import { z } from "zod";
import { tool } from "@langchain/core/tools";

const sayHello = tool(
    async ({ name }) => `Hello ${name}!`,
    {
        name: "say_hello",
        description: "Say hello to someone",
        schema: z.object({ name: z.string() }),
    }
);

const model = new ChatDeepSeek({
    model: "deepseek-chat",
    temperature: 0,
    apiKey: process.env.DEEPSEEK_API_KEY,
});

const modelWithTools = model.bindTools([sayHello]);

console.log("Testing tool calling...");
const response = await modelWithTools.invoke([
    { role: "user", content: "Say hello to World" }
]);

console.log("Response type:", response.constructor.name);
console.log("Has tool_calls:", !!response.tool_calls);
console.log("Tool calls:", JSON.stringify(response.tool_calls));
console.log("Content:", response.content);

if (response.tool_calls && response.tool_calls.length > 0) {
    console.log("PASS: Tool calling works!");
    process.exit(0);
} else {
    console.log("FAIL: No tool calls");
    process.exit(1);
}
