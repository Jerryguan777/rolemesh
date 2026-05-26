/// <reference types="vitest" />
import { defineConfig } from 'vite';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [tailwindcss()],
  root: __dirname,
  build: {
    outDir: `${__dirname}/dist`,
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/ws': {
        target: 'http://localhost:8080',
        ws: true,
      },
      // v1 WS endpoint lives under /api (e.g.
      // /api/v1/conversations/{id}/stream — see ws/v1_client.ts).
      // Production serves SPA + WS on the same origin so the upgrade
      // works automatically; in dev the vite proxy needs `ws: true`
      // or the Upgrade handshake fails silently and the chat panel
      // shows "Disconnected" forever.
      '/api': {
        target: 'http://localhost:8080',
        ws: true,
      },
    },
  },
  test: {
    // Default `node`; component tests opt into happy-dom via the
    // `// @vitest-environment happy-dom` pragma at the top of their
    // file so we only pay the DOM-init cost where it's actually needed.
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
