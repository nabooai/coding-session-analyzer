import { defineConfig } from "vite";

// PONYTAIL: no plugins. Vanilla TS needs none.
// DEV ONLY: proxy same-origin /api calls to the Python API on 8766.
// In PROD the Python server serves ui/dist and answers /api itself, so app fetches stay relative.
export default defineConfig({
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8766",
    },
  },
  build: {
    outDir: "dist",
  },
});
