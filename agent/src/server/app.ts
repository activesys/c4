// c4/agent/src/server/app.ts — Express v5 app setup + middleware
// Mounts all API routes with dependency injection of the agent instance.
// Design: agent.md §3.5 (Web layer)

import express, {
    type Request,
    type Response,
    type NextFunction,
} from "express";
import { existsSync } from "node:fs";
import * as path from "node:path";
import type { C4Agent, AgentStateProvider } from "./types.js";
import { createChatRouter } from "./routes/chat.js";
import { createUploadRouter } from "./routes/upload.js";
import { createServicesRouter } from "./routes/services.js";
import { createStateRouter } from "./routes/state.js";

// ── Application Options ───────────────────────────────────
export interface AppOptions {
    /** C4 Agent instance (provided via dependency injection) */
    agent: C4Agent;
    /** Agent state provider for GET /api/state */
    stateProvider: AgentStateProvider;
    /** CORS origin (from agent.json server.cors_origin) */
    corsOrigin: string;
    /** Mount path for chat router. Default: "/api/chat" */
    chatPath?: string;
    /** Mount path for upload router. Default: "/api/upload" */
    uploadPath?: string;
    /** Mount path for services router. Default: "/api/services" */
    servicesPath?: string;
    /** Mount path for state router. Default: "/api/state" */
    statePath?: string;
    /** Absolute path to the web frontend static dir; served when set and existing (design §4.3 / §5.1) */
    frontendDir?: string;
}

// ── CORS Middleware ───────────────────────────────────────
function createCorsMiddleware(origin: string) {
    return (req: Request, _res: Response, next: NextFunction): void => {
        // Express v5: no callback needed, sync middleware
        // CORS preflight is handled later; this sets headers for all requests
        next();
    };
}

/** Raw CORS handler that sets headers per-request (Express v5 compatible). */
function corsHandler(
    req: Request,
    res: Response,
    next: NextFunction,
    origin: string,
): void {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader(
        "Access-Control-Allow-Methods",
        "GET, POST, PUT, DELETE, OPTIONS",
    );
    res.setHeader(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization, X-Requested-With",
    );
    res.setHeader("Access-Control-Allow-Credentials", "true");

    if (req.method === "OPTIONS") {
        res.status(204).end();
    } else {
        next();
    }
}

// ── createApp ─────────────────────────────────────────────
/**
 * Create and configure the Express v5 application.
 *
 * Middleware (order matters):
 *   1. CORS — permissive for browser-based frontend
 *   2. JSON body parser — for POST /api/chat and any JSON endpoints
 *   3. Routes — mounted with agent dependency injection
 *
 * @param options - Application configuration including agent instance
 * @returns Configured Express application
 */
export function createApp(options: AppOptions): express.Application {
    const app = express();

    const {
        agent,
        stateProvider,
        corsOrigin,
        chatPath = "/api/chat",
        uploadPath = "/api/upload",
        servicesPath = "/api/services",
        statePath = "/api/state",
        frontendDir,
    } = options;

    // 1. CORS — Express v5: no `cors` npm package needed
    const allowedOrigin = corsOrigin || "*";
    app.use((req: Request, res: Response, next: NextFunction) => {
        corsHandler(req, res, next, allowedOrigin);
    });

    // 2. JSON body parser
    app.use(express.json({ limit: "1mb" }));

    // 3. Routes — dependency injected
    app.use(chatPath, createChatRouter(agent));
    app.use(uploadPath, createUploadRouter(agent));
    app.use(servicesPath, createServicesRouter());
    app.use(statePath, createStateRouter(stateProvider));

    // 4. Static frontend hosting (optional; design §4.3 / §5.1)
    if (frontendDir && existsSync(frontendDir)) {
        app.use(express.static(frontendDir));
        app.use((req, res, next) => {
            if (req.method === "GET" && !req.path.startsWith("/api/")) {
                res.sendFile(path.join(frontendDir, "index.html"));
            } else {
                next();
            }
        });
    }

    return app;
}
