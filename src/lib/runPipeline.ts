// Client for the LangGraph agent's HTTP endpoint (agent/src/hazop_agent/server.py,
// `uv run hazop-agent serve`). Uploads the open PDF, runs step 1 → 2 → 3 (and
// consolidation when maxNodes is set) in one request, and streams progress
// back as newline-delimited JSON until the final nodes document arrives.

const AGENT_RUN_URL = import.meta.env.VITE_AGENT_RUN_URL || '/agent/run'

export interface ValidationFinding {
  check: string
  severity: 'error' | 'warning' | 'info'
  detail: string
  source: string
}

export interface PageValidation {
  pageIndex: number
  counts: { error: number; warning: number; info: number }
  findings: ValidationFinding[]
}

export type PipelineEvent =
  /** First event of every run: cached = this drawing's nodes are already on disk. */
  | { type: 'run'; jobId: string; cached: boolean }
  | { type: 'progress'; message: string }
  | { type: 'step'; step: string; status: 'computed' | 'replayed' | 'cached' }
  | {
      type: 'result'
      jobId: string
      fileName: string
      consolidated: boolean
      // Same shape as step3.json / step3-consolidated.json.
      document: unknown
      issues: { step: string; page?: number; problem: string }[]
      /** Step 7 of the node rules, for the rules-based breakdown. */
      validation: PageValidation[]
    }
  | { type: 'error'; message: string }

export async function runPipeline(
  pdf: ArrayBuffer,
  fileName: string,
  maxNodes: number | null,
  onEvent: (event: PipelineEvent) => void,
): Promise<void> {
  const params = new URLSearchParams({ fileName: fileName || 'document.pdf' })
  if (maxNodes) params.set('maxNodes', String(maxNodes))

  const response = await fetch(`${AGENT_RUN_URL}?${params}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/pdf' },
    body: pdf,
  })
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail || `Pipeline request failed (${response.status})`)
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffered = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffered += value
    const lines = buffered.split('\n')
    buffered = lines.pop() ?? ''
    for (const line of lines) if (line.trim()) onEvent(JSON.parse(line) as PipelineEvent)
  }
  if (buffered.trim()) onEvent(JSON.parse(buffered) as PipelineEvent)
}

const SEVERITY_ORDER = ['error', 'warning', 'info'] as const

/** The Step 7 report as chat text: counts first, then findings, errors first. */
export function formatValidation(pages: PageValidation[]): string {
  const lines: string[] = []
  for (const page of pages) {
    const { error, warning, info } = page.counts
    lines.push(`Node-rules validation, page ${page.pageIndex + 1}: ${error} error${error === 1 ? '' : 's'}, ${warning} warning${warning === 1 ? '' : 's'}, ${info} note${info === 1 ? '' : 's'}.`)
    for (const severity of SEVERITY_ORDER) {
      for (const f of page.findings.filter((x) => x.severity === severity)) {
        lines.push(`• [${severity}] ${f.check}: ${f.detail}`)
      }
    }
  }
  return lines.join('\n')
}
