// Client for the agent's chat assistant (agent/src/hazop_agent/chat.py). A
// drawing's thread id is its study / job id: the chat and the pipeline runs
// for a drawing share it, so the conversation comes back with the drawing.

const AGENT_CHAT_URL = import.meta.env.VITE_AGENT_CHAT_URL || '/agent/chat'

export type ChatEvent =
  | { type: 'tool'; name: string; args: Record<string, unknown> }
  /** The node breakdown changed (merge, rename, undo): same shape as step3.json. */
  | { type: 'nodes'; document: unknown }
  | { type: 'message'; text: string }
  | { type: 'error'; message: string }

export interface ChatTurn {
  role: 'user' | 'assistant'
  text: string
}

export async function sendChat(threadId: string, message: string, onEvent: (event: ChatEvent) => void): Promise<void> {
  const response = await fetch(AGENT_CHAT_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ threadId, message }),
  })
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail || `Chat request failed (${response.status})`)
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffered = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffered += value
    const lines = buffered.split('\n')
    buffered = lines.pop() ?? ''
    for (const line of lines) if (line.trim()) onEvent(JSON.parse(line) as ChatEvent)
  }
  if (buffered.trim()) onEvent(JSON.parse(buffered) as ChatEvent)
}

/** The drawing's conversation so far. */
export async function getChatHistory(threadId: string): Promise<ChatTurn[]> {
  const response = await fetch(`${AGENT_CHAT_URL}/${encodeURIComponent(threadId)}`)
  if (!response.ok) throw new Error(`Chat history request failed (${response.status})`)
  return (await response.json()).messages as ChatTurn[]
}

/** What the assistant is doing, for the status line while it works. */
export function describeTool(name: string): string {
  return (
    {
      list_nodes: 'Reading the nodes…',
      show_node: 'Reading a node…',
      merge_nodes: 'Merging nodes…',
      rename_node: 'Renaming a node…',
      undo_last_edit: 'Undoing the last edit…',
      list_edits: 'Reading the edits…',
      validation_report: 'Reading the validation report…',
    }[name] ?? 'Working…'
  )
}
