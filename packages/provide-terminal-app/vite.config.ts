import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  base: "",
  build: {
    outDir: "../../packages/provide-terminal-server/src/provide/terminal/server/frontend",
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
