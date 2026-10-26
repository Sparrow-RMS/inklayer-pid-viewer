// Client for the agent's saved drawings (agent/src/hazop_agent/library.py):
// drawings whose HAZOP pipeline has finished, reopened without uploading
// them again.

import { agentError, agentFetch, agentUrl } from './agentClient'
import type { PageValidation } from './runPipeline'

export interface SavedDrawing {
  jobId: string
  fileName: string
  /** ISO timestamp of the last pipeline step that ran. */
  updatedAt: string | null
  pages: number
  nodes: number
  consolidated: boolean
  /** Worksheet rows across all nodes, and how many nodes have any. */
  deviations: number
  nodesWithWorksheet: number
  has3dView: boolean
}

/** The nodes as the viewer last showed them: same shape as /agent/run's result event. */
export interface SavedNodes {
  jobId: string
  fileName: string | null
  consolidated: boolean
  document: unknown
  validation: PageValidation[]
}

export async function listSavedDrawings(): Promise<SavedDrawing[]> {
  const response = await agentFetch(agentUrl('/agent/jobs'))
  if (!response.ok) throw new Error(await agentError(response, "Couldn't load your drawings"))
  return response.json()
}

/** The drawing's PDF and its nodes, fetched together. */
export async function openSavedDrawing(jobId: string): Promise<{ pdf: ArrayBuffer; nodes: SavedNodes }> {
  const id = encodeURIComponent(jobId)
  const [pdf, nodes] = await Promise.all([agentFetch(agentUrl(`/agent/jobs/${id}/source.pdf`)), agentFetch(agentUrl(`/agent/jobs/${id}/nodes`))])
  if (!pdf.ok) throw new Error(await agentError(pdf, "Couldn't load the drawing"))
  if (!nodes.ok) throw new Error(await agentError(nodes, "Couldn't load its nodes"))
  return { pdf: await pdf.arrayBuffer(), nodes: await nodes.json() }
}
