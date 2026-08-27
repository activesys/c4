// c4/test/web/playwright.config.ts
// L3 端到端测试配置（Playwright）— c4/test/web/README.md §2.2, §5, §7。
//
// webServer 数组（按序启动）：
//   1. 后端 c4_agent @ 127.0.0.1:3000 — 由 e2e/start-backend.mjs 制备一次性配置
//      目录后启动；就绪判定为 GET /api/services 返回 200（registry 加载完成前
//      后端返回 503，Playwright 会持续轮询直到 2xx）。
//   2. 前端 Vite dev server @ 5173（c4/agent/frontend）— 其 /api/* 代理指向
//      http://localhost:3000（见 vite.config.ts）。
//
// 测试串行执行（workers: 1）：Agent 的跨轮状态保存在内存闭包中（web.md §3.1.2），
// 三个场景共享同一个后端实例，顺序即 README §5.1 的场景顺序。
//
// LLM 依赖：场景 5.1.1 / 5.1.3 的对话部分以 DEEPSEEK_API_KEY 门控（spec 内
// test.skip），无 key 时 5.1.2 仍可运行。

import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const frontendDir = fileURLToPath(new URL("../../agent/frontend", import.meta.url));

export default defineConfig({
    testDir: "./e2e",
    timeout: 900_000, // LLM 轮次可达 30-60s，多轮流程放宽上限
    expect: { timeout: 30_000 },
    fullyParallel: false,
    workers: 1,
    retries: 0,
    reporter: [["list"]],
    outputDir: "./test-results",
    use: {
        baseURL: "http://localhost:5173",
        trace: "retain-on-failure",
        screenshot: "only-on-failure",
        video: "retain-on-failure",
    },
    webServer: [
        {
            command: "node e2e/start-backend.mjs",
            cwd: here,
            url: "http://127.0.0.1:3000/api/services",
            timeout: 240_000,
            reuseExistingServer: false,
        },
        {
            command: "npm run dev",
            cwd: frontendDir,
            url: "http://localhost:5173",
            timeout: 120_000,
            reuseExistingServer: !process.env.CI,
        },
    ],
    globalTeardown: "./e2e/teardown.mjs",
});
