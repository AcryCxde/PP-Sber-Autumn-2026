import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// BACKEND_URL — для dev-сервера рядом с уже запущенным docker-стеком на 8000.
const backendUrl = process.env.BACKEND_URL ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: backendUrl,
        changeOrigin: true,
      },
      '/ws': {
        target: backendUrl.replace(/^http/, 'ws'),
        ws: true,
      },
    },
  },
})
