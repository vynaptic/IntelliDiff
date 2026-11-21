import { defineConfig } from 'vite';

// The API serves web/dist at /; in development the dev server forwards /v1 to it.
// A bundled font's licence text keeps a name that a browser shows as text.
const assetFileNames = (asset: { names: string[] }) =>
  asset.names.includes('LICENSE') ? 'assets/[name]-[hash].txt' : 'assets/[name]-[hash][extname]';

export default defineConfig({
  build: { rollupOptions: { output: { assetFileNames } } },
  server: { proxy: { '/v1': 'http://127.0.0.1:8000' } },
});
