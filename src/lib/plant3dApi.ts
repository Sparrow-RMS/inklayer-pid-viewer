// Client for the agent server's 3D plant view (agent/src/hazop_agent/plant3d.py):
// uploads the open PDF, Claude writes a Three.js page of the plant, and the
// server keeps it beside the drawing's other results.

const PLANT3D_URL = import.meta.env.VITE_PLANT3D_URL || '/agent/plant3d'

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

  const response = await fetch(`${PLANT3D_URL}?${params}`, {
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
