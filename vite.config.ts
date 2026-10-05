import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: ['rewrite-semicolon-playmate.ngrok-free.dev'],
    proxy: {
      // The LangGraph agent (agent/, `uv run hazop-agent serve`).
      '/agent': {
        target: 'http://127.0.0.1:4002',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://127.0.0.1:4001',
        changeOrigin: true,
      },
    },
  },
})
