// c4/test/web/e2e/teardown.mjs
// E2E 全局清理（Playwright globalTeardown）— README §7。
//
// 后端进程由 Playwright webServer 关闭，这里做尽力而为的收尾：
//   1. 从 config.json 收集 shm_id 并 ipcrm 清理共享内存段（对齐 python/conftest.py
//      的 _collect_shm_ids_from_config / _cleanup_shm_ids）；
//   2. 删除一次性配置目录与标记文件。

import { existsSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";

export default function globalTeardown() {
    const marker = path.join(tmpdir(), "c4-e2e-config-dir.txt");
    if (!existsSync(marker)) {
        return;
    }
    const configDir = readFileSync(marker, "utf-8").trim();

    // 1. 清理共享内存段
    const configPath = path.join(configDir, "config.json");
    if (existsSync(configPath)) {
        try {
            const config = JSON.parse(readFileSync(configPath, "utf-8"));
            const shmIds = new Set();
            for (const [key, value] of Object.entries(config)) {
                if (key === "c4_shm_manager" || !Array.isArray(value)) {
                    continue;
                }
                for (const instance of value) {
                    for (const point of instance?.points ?? []) {
                        const shmId = point?.shm_id ?? 0;
                        if (shmId > 0) {
                            shmIds.add(shmId);
                        }
                    }
                }
            }
            for (const shmId of shmIds) {
                try {
                    execFileSync("ipcrm", ["-M", String(shmId)]);
                } catch {
                    // 段可能已被后端/系统回收 — 忽略
                }
            }
        } catch {
            // config.json 可能不存在或格式异常 — 忽略
        }
    }

    // 2. 删除临时配置目录与标记文件
    try {
        rmSync(configDir, { recursive: true, force: true });
    } catch {
        // 忽略
    }
    try {
        rmSync(marker, { force: true });
    } catch {
        // 忽略
    }
}
