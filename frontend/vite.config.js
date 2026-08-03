import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Build output goes to ../webui-dist, the only frontend served by the Python
// application. Dev proxies API + SSE + media to the backend on :8770.
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: { outDir: '../webui-dist', emptyOutDir: true },
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8770', changeOrigin: true, ws: true },
      '/feishu': { target: 'http://127.0.0.1:8770', changeOrigin: true },
    },
  },
})
