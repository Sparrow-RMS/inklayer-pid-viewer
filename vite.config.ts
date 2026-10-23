import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // The agent's port, from agent/.env (or the shell), the same setting
  // `uv run hazop-agent serve` listens on.
  const agentEnv = loadEnv(mode, fileURLToPath(new URL('./agent', import.meta.url)), 'AGENT_')
  const agentPort = agentEnv.AGENT_PORT || '4002'

  return {
    plugins: [react()],
    server: {
      allowedHosts: ['rewrite-semicolon-playmate.ngrok-free.dev'],
      proxy: {
        // The LangGraph agent (agent/, `uv run hazop-agent serve`).
        '/agent': {
          target: `http://127.0.0.1:${agentPort}`,
          changeOrigin: true,
        },
      },
    },
  }
})
