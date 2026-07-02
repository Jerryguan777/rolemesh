/// <reference types="vitest" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()], // Tailwind 3 runs via PostCSS, not a Vite plugin
  root: __dirname,
  build: {
    outDir: `${__dirname}/dist`,
    emptyOutDir: true,
  },
  server: {
    port: 5174, // web/ (Lit SPA) uses 5173; run both side by side
    proxy: {
      // ws:true on /api is REQUIRED: the v1 stream endpoint is
      // /api/v1/conversations/{id}/stream — without it the Upgrade
      // handshake fails silently and chat shows "Disconnected"
      // forever (same note as web/vite.config.ts).
      '/api': { target: 'http://localhost:8080', ws: true },
      // The OIDC callback bridge page (GET /oauth2/callback) writes
      // the auth code into sessionStorage, which is per-origin — in
      // dev it must be served through the Vite origin or the SPA
      // cannot read it.
      '/oauth2': { target: 'http://localhost:8080' },
    },
  },
  test: {
    // Default `node`; component/DOM tests opt into happy-dom via the
    // `// @vitest-environment happy-dom` pragma so we only pay the
    // DOM-init cost where it's actually needed (same policy as web/).
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
