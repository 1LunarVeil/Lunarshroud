import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath, URL } from 'node:url';

/**
 * Build target is `ui/dist`, which is what Tauri embeds (`frontendDist`) and what
 * the FastAPI backend serves in source mode. Two constraints shape this file:
 *
 * 1. **Offline.** No CDN, no remote font, no remote chunk. Everything is bundled
 *    or copied from `public/`. `backend/tests/test_offline_contract.py` asserts it.
 * 2. **Relative base.** Tauri loads the app from `tauri.localhost` via the asset
 *    protocol while the backend lives on `http://127.0.0.1:<port>`, so the built
 *    assets must not assume they are served from the origin root.
 */
export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    // The message list is the hot path; a single oversized vendor chunk delays
    // first paint of the shell. Keep the warning threshold honest but low enough
    // that a regression shows up instead of hiding under 500 kB.
    chunkSizeWarningLimit: 900,
    // **No `manualChunks`.** An explicit split of React, Radix and TanStack into
    // named chunks produced a load order in which one vendor chunk wrote to the
    // React namespace before React itself had initialised:
    //   TypeError: Cannot set properties of undefined (setting 'Activity')
    // and the app rendered nothing. Vite's automatic chunking preserves the
    // module graph's initialisation order, which is the property that matters here;
    // the physics of "smaller chunks" is not worth a blank window.
    //
    // Code splitting still happens where it is safe and valuable: every page is a
    // dynamic `import()`, and each syntax-highlighting grammar is its own chunk.
  },
  server: {
    port: 5173,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.ts', 'tests/**/*.test.tsx'],
    exclude: ['tests/browser/**'],
    globals: false,
  },
});
