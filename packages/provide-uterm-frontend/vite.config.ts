import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
  build: {
    outDir: '../../packages/provide-uterm-server/src/provide/uterm/server/frontend',
    emptyOutDir: false,
    manifest: 'vanilla-manifest.json',
    rollupOptions: {
      input: {
        hijack: resolve(__dirname, 'src/hijack.ts'),
        terminal: resolve(__dirname, 'src/terminal.ts')
      }
    }
  }
});
