import { defineConfig } from 'vite';

// The API serves web/dist at /; in development the dev server forwards /v1 to it.
export default defineConfig({
  server: { proxy: { '/v1': 'http://127.0.0.1:8000' } },
});
