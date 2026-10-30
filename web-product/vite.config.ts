import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// BACKEND_URL: the dev server proxies /api to an already running backend (docker stack on 8000).
const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  base: "./",
  server: { proxy: { "/api": { target: backendUrl, changeOrigin: true } } },
  preview: { proxy: { "/api": { target: backendUrl, changeOrigin: true } } },
});
