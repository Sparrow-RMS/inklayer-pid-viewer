// Client for the agent server's 3D nodes scene (agent/src/hazop_agent/plant_scene.py):
// the pipeline's own entities and traced pipes, with Claude's sizing of the
// equipment, keyed by the ids the HAZOP nodes list.

import { agentFetch, agentUrl } from './agentClient'

const PLANT_SCENE_URL = import.meta.env.VITE_PLANT_SCENE_URL || agentUrl('/agent/plant-scene')

export type ItemShape =
  | 'vertical_vessel' | 'column' | 'tank' | 'horizontal_vessel' | 'heat_exchanger' | 'pump' | 'compressor'
  | 'filter' | 'agitator' | 'motor' | 'heater' | 'skid' | 'other'
  | 'valve' | 'instrument' | 'connector' | 'fitting'

export interface SceneItem {
  /** Step 1 entity id, as in a node's entityIds. */
  id: string
  category: 'equipment' | 'valve' | 'instrument' | 'connector' | 'fitting'
  tag: string | null
  type: string
  /** Centre and drawn size, in page fractions (origin top-left). */
  x: number
  y: number
  w: number
  h: number
  shape: ItemShape
  diameterM: number
  heightM: number
  /** Null: at pipe height. */
  elevationM: number | null
  /** Who decided the 3D form: Claude, or a default from the type text. */
  sizedBy: 'claude' | 'type'
}

export interface SceneSpan {
  /** Traced span id, as in a node's routes[].entityId. */
  id: string
  points: { x: number; y: number }[]
  lineNumbers: string[]
}

export interface ScenePage {
  pageIndex: number
  /** Page height / width. */
  aspect: number
  /** Metres across the sheet. */
  pageWidthM: number
  items: SceneItem[]
  spans: SceneSpan[]
}

export interface PlantScene {
  jobId: string
  /** Null until Claude has sized the equipment for the current step 1 run. */
  forms: { model: string; generatedAt: string } | null
  formsStale: boolean
  pages: ScenePage[]
}

export class SceneNotReadyError extends Error {}

async function read(response: Response): Promise<PlantScene> {
  if (response.ok) return response.json()
  const body = await response.json().catch(() => null)
  const message = body?.detail || `3D nodes request failed (${response.status})`
  throw response.status === 404 ? new SceneNotReadyError(message) : new Error(message)
}

export async function getPlantScene(jobId: string): Promise<PlantScene> {
  return read(await agentFetch(`${PLANT_SCENE_URL}/${encodeURIComponent(jobId)}`))
}

/** Has Claude size the equipment (cached server-side), then returns the scene. */
export async function sizePlantScene(jobId: string, force = false): Promise<PlantScene> {
  const params = force ? '?force=true' : ''
  return read(await agentFetch(`${PLANT_SCENE_URL}/${encodeURIComponent(jobId)}${params}`, { method: 'POST' }))
}
