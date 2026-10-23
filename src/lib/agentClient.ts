/**
 * Where the agent server is, and how to call it. Every client in src/lib goes
 * through here, so pointing the viewer at another agent is one setting.
 *
 * Build-time settings (a .env file at the repo root, or the build's environment):
 *
 *   VITE_AGENT_URL       the agent's public base URL, e.g. https://agent.example.com
 *                        (empty = same site as the viewer, /agent proxied to it)
 *   VITE_AGENT_API_KEY   one of the agent's AGENT_API_KEYS, sent with every call
 *
 * A key built into the viewer can be read by anyone who can load the viewer:
 * it identifies this app to the agent, it doesn't protect the agent from
 * people who can open the app. Keep the viewer behind your sign-in.
 */

const AGENT_URL = (import.meta.env.VITE_AGENT_URL || '').replace(/\/+$/, '')
const API_KEY = import.meta.env.VITE_AGENT_API_KEY || ''

/** The full URL of an agent path, e.g. agentUrl('/agent/run'). */
export function agentUrl(path: string): string {
  return `${AGENT_URL}${path}`
}

/** fetch() with the agent's API key added. Takes a full URL (see agentUrl). */
export function agentFetch(url: string, init: RequestInit = {}): Promise<Response> {
  if (!API_KEY) return fetch(url, init)
  const headers = new Headers(init.headers)
  headers.set('Authorization', `Bearer ${API_KEY}`)
  return fetch(url, { ...init, headers })
}

/** The error message an agent response carries, or a fallback. */
export async function agentError(response: Response, fallback: string): Promise<string> {
  const body = await response.json().catch(() => null)
  const detail = typeof body?.detail === 'string' ? body.detail : body?.detail?.[0]?.msg
  if (response.status === 401) return 'The agent rejected this app\'s API key (check VITE_AGENT_API_KEY against AGENT_API_KEYS).'
  return detail || `${fallback} (${response.status})`
}

/**
 * Downloads an agent file with the key and opens it in a new tab. A plain
 * link can't carry the key. Only for the team's own uploads (study package
 * documents), never for model-written pages: a blob URL runs with this app's origin.
 */
export async function openAgentFile(url: string, fileName: string): Promise<void> {
  // Opened first, while the click still counts as the user's: a tab opened after an await is blocked.
  const tab = window.open('', '_blank')
  const response = await agentFetch(url)
  if (!response.ok) {
    tab?.close()
    throw new Error(await agentError(response, `Couldn't open ${fileName}`))
  }
  const blobUrl = URL.createObjectURL(await response.blob())
  if (tab) tab.location.href = blobUrl
  else window.location.assign(blobUrl)
  // Long enough for the tab to load it.
  setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000)
}
