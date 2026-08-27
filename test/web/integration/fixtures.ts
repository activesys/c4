// @vitest-environment node
// L2 integration fixture — starts a real c4_agent, mirroring c4/test/web/python/conftest.py:
// write agent.json → prepare mcp-registry → spawn agent (--config-dir) → poll readiness → teardown.

import { spawn, execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const PROJECT_ROOT = fileURLToPath(new URL("../../..", import.meta.url));
const AGENT_ENTRY = join(PROJECT_ROOT, "agent", "dist", "index.js");
const REGISTRY_SRC = join(PROJECT_ROOT, "config", "mcp-registry");
const SHM_MANAGER_BINARY = "/usr/local/bin/c4_shm_manager";

const READY_TIMEOUT_MS = 60_000;
const READY_POLL_MS = 500;

export interface AgentHandle {
  baseUrl: string;
  port: number;
  stop: () => Promise<void>;
}

type ChildProcess = ReturnType<typeof spawn>;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      const port = typeof addr === "object" && addr !== null ? addr.port : 0;
      server.close(() => resolve(port));
    });
  });
}

function waitForExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    if (child.exitCode !== null || child.signalCode !== null) {
      resolve(true);
      return;
    }
    const timer = setTimeout(() => resolve(false), timeoutMs);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve(true);
    });
  });
}

function buildAgentConfig(
  registryDir: string,
  configDir: string,
  port: number,
): Record<string, unknown> {
  return {
    instance_id: "c4_test",
    model: {
      provider: "deepseek",
      name: "deepseek-chat",
      temperature: 0,
      max_tokens: 4096,
      api_key_env: "DEEPSEEK_API_KEY",
    },
    server: { host: "127.0.0.1", port, cors_origin: "*" },
    mcp_registry: { path: registryDir },
    shm_manager: {
      binary: SHM_MANAGER_BINARY,
      config_path: join(configDir, "config.json"),
    },
    state: { backend: "filesystem", path: join(configDir, "state") },
    logging: { level: "info", dir: join(configDir, "logs") },
  };
}

async function prepareRegistryDir(tmpDir: string): Promise<string> {
  const registryDir = join(tmpDir, "mcp-registry");
  await mkdir(registryDir, { recursive: true });
  for (const file of await readdir(REGISTRY_SRC)) {
    if (!file.endsWith(".json")) continue;
    const data = JSON.parse(await readFile(join(REGISTRY_SRC, file), "utf-8"));
    const serviceType: unknown = data.service_type;
    if (typeof serviceType === "string") {
      const candidate = `/usr/local/bin/${serviceType}`;
      if (existsSync(candidate)) {
        data.binary_path = candidate;
      }
    }
    await writeFile(join(registryDir, file), JSON.stringify(data, null, 2));
  }
  return registryDir;
}

// The frontend api layer calls fetch("/api/...") with relative URLs (web.md §4.3);
// Node's fetch rejects relative URLs, so route them to the live agent's baseUrl.
let originalFetch: typeof fetch | null = null;

function installFetchProxy(baseUrl: string): void {
  originalFetch = globalThis.fetch;
  const realFetch = originalFetch.bind(globalThis);
  const proxied = (input: unknown, init?: RequestInit): Promise<Response> => {
    let url: string;
    if (typeof input === "string") {
      url = input;
    } else if (input instanceof URL) {
      url = input.toString();
    } else if (input !== null && typeof input === "object" && "url" in input) {
      url = String((input as { url: unknown }).url);
    } else {
      url = String(input);
    }
    if (url.startsWith("/")) {
      return realFetch(`${baseUrl}${url}`, init);
    }
    return realFetch(input as Parameters<typeof fetch>[0], init);
  };
  globalThis.fetch = proxied as typeof fetch;
}

function restoreFetch(): void {
  if (originalFetch !== null) {
    globalThis.fetch = originalFetch;
    originalFetch = null;
  }
}

async function waitReady(baseUrl: string): Promise<void> {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${baseUrl}/api/services`);
      if (res.status === 200) return;
      lastError = new Error(`HTTP ${res.status}`);
    } catch (err) {
      lastError = err;
    }
    await sleep(READY_POLL_MS);
  }
  throw new Error(
    `Agent 未能在 ${READY_TIMEOUT_MS}ms 内就绪（last error: ${String(lastError)}）`,
  );
}

function collectShmIds(config: Record<string, unknown>): number[] {
  const ids: number[] = [];
  for (const [key, value] of Object.entries(config)) {
    if (key === "c4_shm_manager") continue;
    if (!Array.isArray(value)) continue;
    for (const instance of value) {
      if (typeof instance !== "object" || instance === null) continue;
      const points = (instance as Record<string, unknown>).points;
      if (!Array.isArray(points)) continue;
      for (const pt of points) {
        if (typeof pt !== "object" || pt === null) continue;
        const sid = (pt as Record<string, unknown>).shm_id;
        if (typeof sid === "number" && sid > 0) ids.push(sid);
      }
    }
  }
  return ids;
}

async function cleanupShm(configDir: string): Promise<void> {
  const configPath = join(configDir, "config.json");
  if (!existsSync(configPath)) return;
  try {
    const config = JSON.parse(await readFile(configPath, "utf-8")) as Record<
      string,
      unknown
    >;
    await Promise.all(
      collectShmIds(config).map(
        (id) =>
          new Promise<void>((resolve) => {
            execFile("ipcrm", ["-M", String(id)], () => resolve());
          }),
      ),
    );
  } catch {
    // teardown must never throw
  }
}

export async function startAgent(): Promise<AgentHandle> {
  const tmpDir = await mkdtemp(join(tmpdir(), "c4-web-test-"));
  const configDir = join(tmpDir, "config");
  await mkdir(configDir, { recursive: true });
  const registryDir = await prepareRegistryDir(tmpDir);
  const port = await findFreePort();

  await writeFile(
    join(configDir, "agent.json"),
    JSON.stringify(buildAgentConfig(registryDir, configDir, port), null, 2),
  );

  const baseUrl = `http://127.0.0.1:${port}`;

  const child = spawn("node", [AGENT_ENTRY, "--config-dir", configDir], {
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stdoutBuf = "";
  let stderrBuf = "";
  child.stdout?.on("data", (chunk: Buffer) => {
    stdoutBuf += chunk.toString();
  });
  child.stderr?.on("data", (chunk: Buffer) => {
    stderrBuf += chunk.toString();
  });

  installFetchProxy(baseUrl);

  try {
    await waitReady(baseUrl);
  } catch (err) {
    restoreFetch();
    child.kill("SIGKILL");
    const detail = err instanceof Error ? err.message : String(err);
    throw new Error(
      `${detail}\n--- agent stdout (tail) ---\n${stdoutBuf.slice(-2000)}\n` +
        `--- agent stderr (tail) ---\n${stderrBuf.slice(-2000)}`,
    );
  }

  let stopped = false;
  const stop = async (): Promise<void> => {
    if (stopped) return;
    stopped = true;
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      const exited = await waitForExit(child, 10_000);
      if (!exited) {
        child.kill("SIGKILL");
        await waitForExit(child, 5_000);
      }
    }
    await cleanupShm(configDir);
    await rm(tmpDir, { recursive: true, force: true }).catch(() => undefined);
    restoreFetch();
  };

  return { baseUrl, port, stop };
}
