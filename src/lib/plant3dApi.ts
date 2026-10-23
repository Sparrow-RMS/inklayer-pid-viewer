// Client for the agent server's 3D plant view (agent/src/hazop_agent/plant3d.py):
// uploads the open PDF, Claude writes a Three.js page of the plant, and the
// server keeps it beside the drawing's other results.

import { agentError, agentFetch, agentUrl } from './agentClient'

const PLANT3D_URL = import.meta.env.VITE_PLANT3D_URL || agentUrl('/agent/plant3d')

export type Plant3dEvent =
  /** First event: cached = the page is already on disk and no model call is made. */
  | { type: 'start'; jobId: string; cached: boolean }
  /** A piece of Claude's reasoning summary, in order. */
  | { type: 'thinking'; text: string }
  /** Characters of the page written so far. */
  | { type: 'progress'; chars: number }
  | { type: 'result'; jobId: string; url: string }
  | { type: 'error'; message: string }

export async function generatePlant3d(
  pdf: ArrayBuffer,
  fileName: string,
  force: boolean,
  onEvent: (event: Plant3dEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const params = new URLSearchParams({ fileName: fileName || 'document.pdf' })
  if (force) params.set('force', 'true')

  const response = await agentFetch(`${PLANT3D_URL}?${params}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/pdf' },
    // pdfjs may detach the buffer it was handed, so send a copy.
    body: pdf.slice(0),
    signal,
  })
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail || `3D view request failed (${response.status})`)
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffered = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffered += value
    const lines = buffered.split('\n')
    buffered = lines.pop() ?? ''
    for (const line of lines) if (line.trim()) onEvent(JSON.parse(line) as Plant3dEvent)
  }
  if (buffered.trim()) onEvent(JSON.parse(buffered) as Plant3dEvent)
}

/** The generated page's HTML, fetched with the API key (an iframe src or a link can't send it). */
export async function fetchPlant3dPage(url: string): Promise<string> {
  const response = await agentFetch(agentUrl(url))
  if (!response.ok) throw new Error(await agentError(response, "Couldn't load the 3D view"))
  return response.text()
}

/**
 * Opens a generated page in a new tab, still sandboxed: the tab holds only an
 * iframe allowed to run scripts, so the model-written code never gets this
 * app's origin (as it would loaded straight from a blob URL).
 */
export function openPlant3dInNewTab(html: string, title: string) {
  const tab = window.open('', '_blank')
  if (!tab) return
  tab.document.title = title
  const style = tab.document.createElement('style')
  style.textContent = 'html,body{margin:0;height:100%;background:#0b1020}iframe{border:0;width:100%;height:100%;display:block}'
  tab.document.head.append(style)
  const frame = tab.document.createElement('iframe')
  frame.setAttribute('sandbox', 'allow-scripts')
  frame.title = title
  frame.srcdoc = html
  tab.document.body.append(frame)
}
