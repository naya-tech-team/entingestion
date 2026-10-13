import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Local development (`make ui-dev`): the dev server serves the SPA on UI_DEV_PORT and proxies
// /api/* to the deployed CloudFront distribution, adding the basic-auth header CloudFront
// expects. The browser still only talks to its own origin, as in production.
//   UI_API_TARGET  the distribution's URL (the ui stack's `url` output; no trailing slash)
//   UI_USER, UI_PASSWORD  the static login (never committed; read from the environment)
//   UI_DEV_PORT  the dev server's port (default 5173; the dev raw bucket's CORS must allow it)
const DEFAULT_DEV_PORT = 5173;
const target = process.env.UI_API_TARGET;
const devPort = parsePort(process.env.UI_DEV_PORT) ?? DEFAULT_DEV_PORT;
const user = process.env.UI_USER;
const password = process.env.UI_PASSWORD;
const proxyHeaders: Record<string, string> =
  user && password
    ? { Authorization: `Basic ${Buffer.from(`${user}:${password}`, "utf8").toString("base64")}` }
    : {};

function parsePort(value: string | undefined): number | null {
  if (!value) return null;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error(`UI_DEV_PORT must be a port number, got ${value}`);
  return port;
}

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
    sourcemap: false,
    // Modern browsers support modulepreload; the polyfill is not needed.
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
  server: {
    port: devPort,
    strictPort: true,
    proxy: target
      ? {
          "/api": {
            target,
            changeOrigin: true,
            secure: true,
            headers: proxyHeaders,
          },
        }
      : undefined,
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
