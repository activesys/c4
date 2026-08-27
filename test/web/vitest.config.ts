import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const frontendSrc = fileURLToPath(new URL("../../agent/frontend/src", import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@frontend": frontendSrc,
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: [fileURLToPath(new URL("./vitest.setup.ts", import.meta.url))],
    include: ["unit/**/*.test.{ts,tsx}", "integration/**/*.test.{ts,tsx}"],
    testTimeout: 180000,
    hookTimeout: 120000,
    css: false,
  },
});
