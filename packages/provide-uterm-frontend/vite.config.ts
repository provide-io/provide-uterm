import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
  base: './',
  build: {
    outDir: '../../packages/provide-uterm-server/src/provide/uterm/server/frontend',
    emptyOutDir: false,
    manifest: 'vanilla-manifest.json',
    rollupOptions: {
      input: {
        hijack_script: resolve(__dirname, 'src/hijack.ts'),
        terminal_script: resolve(__dirname, 'src/terminal.ts'),
        vnc_script: resolve(__dirname, 'src/vnc-page.ts'),
        panels_script: resolve(__dirname, 'src/panels.ts'),
        hijack: resolve(__dirname, 'hijack.html'),
        terminal: resolve(__dirname, 'terminal.html'),
        vnc: resolve(__dirname, 'vnc.html'),
        panels: resolve(__dirname, 'panels.html')
      }
    }
  }
});
