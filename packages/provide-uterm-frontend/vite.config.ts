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
        hijack: resolve(__dirname, 'hijack.html'),
        terminal: resolve(__dirname, 'terminal.html')
      }
    }
  }
});
