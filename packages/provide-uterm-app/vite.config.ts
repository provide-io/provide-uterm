import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev-server proxy target for the local FastAPI backend. Kept out of the inline
// proxy config (no hardcoded ports) and overridable via env; defaults to the
// FastAPI dev port (27780).
const DEV_API_HOST = process.env.UTERM_DEV_API_HOST ?? "localhost:27780";
const DEV_HTTP_TARGET = `http://${DEV_API_HOST}`;
const DEV_WS_TARGET = `ws://${DEV_API_HOST}`;

export default defineConfig({
  plugins: [react()],
  base: "",
  build: {
    outDir: "../../packages/provide-uterm-server/src/provide/uterm/server/frontend",
    emptyOutDir: false,
    manifest: true,
    rollupOptions: {
      input: "src/main.tsx",
    },
  },
  server: {
    proxy: {
      "/api": DEV_HTTP_TARGET,
      "/ws": { target: DEV_WS_TARGET, ws: true },
    },
  },
});
