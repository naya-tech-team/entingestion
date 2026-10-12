import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Local development (`make ui-dev`): the dev server serves the SPA on :5173 and proxies /api/* to
// the deployed CloudFront distribution, adding the basic-auth header CloudFront expects. The
// browser still only talks to its own origin, as in production.
//   UI_API_TARGET  e.g. https://dxxxx.cloudfront.net (no trailing slash)
//   UI_USER, UI_PASSWORD  the static login (never committed; read from the environment)
const target = process.env.UI_API_TARGET;
const user = process.env.UI_USER;
const password = process.env.UI_PASSWORD;
const proxyHeaders: Record<string, string> =
  user && password
    ? { Authorization: `Basic ${Buffer.from(`${user}:${password}`, "utf8").toString("base64")}` }
    : {};

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
    port: 5173,
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
