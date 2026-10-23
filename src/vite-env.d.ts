/// <reference types="vite/client" />

// Build settings the viewer reads (see .env.example).
interface ImportMetaEnv {
  /** The agent's public base URL; empty = same site, /agent proxied. */
  readonly VITE_AGENT_URL?: string
  /** One of the agent's AGENT_API_KEYS. Readable by anyone who loads the viewer. */
  readonly VITE_AGENT_API_KEY?: string
  /** Older per-endpoint overrides, still honoured: full URLs. */
  readonly VITE_AGENT_RUN_URL?: string
  readonly VITE_AGENT_CHAT_URL?: string
  readonly VITE_HAZOP_API_URL?: string
  readonly VITE_PLANT3D_URL?: string
  readonly VITE_PLANT_SCENE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
