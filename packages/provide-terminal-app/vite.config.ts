import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  base: "",
  resolve: {
    alias: {
      "@provide-terminal-frontend": path.resolve(__dirname, "../provide-terminal-frontend/src"),
    },
  },
  build: {
    outDir: "../../packages/provide-terminal/src/provide/terminal/frontend",
    emptyOutDir: false,
    manifest: true,
    rollupOptions: {
      input: "src/main.tsx",
    },
  },
  server: {
    proxy: {
      "/api": "http://localhost:27780",
      "/ws": { target: "ws://localhost:27780", ws: true },
    },
  },
});
