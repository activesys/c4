// c4/test/web/e2e/start-backend.mjs
// E2E 后端启动脚本（Playwright webServer 第一个条目）— README §2.2, §5, §7。
//
// 职责：
//   1. 制备一次性配置目录（agent.json + mcp-registry 副本），移植
//      c4/test/web/python/conftest.py 的 write_agent_json / registry_dir 等价逻辑；
//   2. 以子进程方式启动真实后端 `node c4/agent/dist/index.js --config-dir <dir>`
//      （监听 127.0.0.1:3000，与前端 vite 代理目标一致）；
//   3. 转发 SIGTERM/SIGINT 给后端，保证 Playwright 关闭 webServer 时进程树被回收。
//
// 后端就绪判定由 playwright.config.ts 的 webServer.url 完成：
// GET /api/services 在 registry 加载完成前返回 503，之后返回 200。

import { spawn } from "node:child_process";
import {
    existsSync,
    mkdirSync,
    readdirSync,
    readFileSync,
    writeFileSync,
    mkdtempSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const C4_ROOT = path.resolve(HERE, "..", "..", ".."); // c4/
const REGISTRY_SRC = path.join(C4_ROOT, "config", "mcp-registry");
const AGENT_JS =
    process.env.C4_AGENT_PATH || path.join(C4_ROOT, "agent", "dist", "index.js");
const SHM_MANAGER =
    process.env.C4_SHM_MANAGER_PATH || "/usr/local/bin/c4_shm_manager";
const PORT = 3000; // 必须与 c4/agent/frontend/vite.config.ts 的代理目标一致

// ── 1. 一次性配置目录 ────────────────────────────────
const configDir = mkdtempSync(path.join(tmpdir(), "c4-e2e-"));

// ── 2. mcp-registry 副本：修正 binary_path 为实际产物路径 ──
const registryDir = path.join(configDir, "mcp-registry");
mkdirSync(registryDir, { recursive: true });
if (existsSync(REGISTRY_SRC)) {
    for (const name of readdirSync(REGISTRY_SRC).filter((f) => f.endsWith(".json"))) {
        const entry = JSON.parse(readFileSync(path.join(REGISTRY_SRC, name), "utf-8"));
        const candidate = `/usr/local/bin/${entry.service_type}`;
        if (existsSync(candidate)) {
            entry.binary_path = candidate;
        }
        writeFileSync(
            path.join(registryDir, name),
            JSON.stringify(entry, null, 2),
            "utf-8",
        );
    }
}

// ── 3. agent.json（镜像 python/conftest.py write_agent_json）──
const agentConfig = {
    instance_id: "c4_e2e",
    model: {
        provider: "deepseek",
        name: "deepseek-chat",
        temperature: 0,
        max_tokens: 4096,
        api_key_env: "DEEPSEEK_API_KEY",
    },
    server: {
        host: "127.0.0.1",
        port: PORT,
        cors_origin: "*",
    },
    mcp_registry: {
        path: registryDir,
    },
    shm_manager: {
        binary: SHM_MANAGER,
        config_path: path.join(configDir, "config.json"),
    },
    state: {
        backend: "filesystem",
        path: path.join(configDir, "state"),
    },
    logging: {
        level: "info",
        dir: path.join(configDir, "logs"),
    },
};
writeFileSync(
    path.join(configDir, "agent.json"),
    JSON.stringify(agentConfig, null, 2),
    "utf-8",
);

// 供 teardown.mjs 回收共享内存段与临时目录
writeFileSync(path.join(tmpdir(), "c4-e2e-config-dir.txt"), configDir, "utf-8");

console.error(`[e2e] backend config dir: ${configDir}`);

// ── 4. 启动后端（子进程，转发终止信号）────────────────
const child = spawn(process.execPath, [AGENT_JS, "--config-dir", configDir], {
    stdio: "inherit",
});

for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) {
    process.on(signal, () => {
        child.kill(signal);
    });
}

child.on("exit", (code, signal) => {
    process.exit(code ?? (signal ? 1 : 0));
});
