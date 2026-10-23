// How far the 3D view's build has got, shared by the construction site and the build panel.

// Pages run roughly 30–80k characters; progress approaches 1 over that range.
const TYPICAL_PAGE_CHARS = 35000
export function progressFor(chars: number) {
  return chars > 0 ? 1 - Math.exp(-chars / TYPICAL_PAGE_CHARS) : 0
}

export const clamp = (v: number) => Math.max(0, Math.min(1, v))
// Each piece of the site finishes at its own point of the build.
export const tankPart = (p: number) => clamp(p / 0.45)
export const columnPart = (p: number) => clamp((p - 0.2) / 0.5)
export const pipingPart = (p: number) => clamp((p - 0.45) / 0.45)

export const SITE_STAGES = [
  { label: 'Planning', detail: 'Reading the drawing and planning the layout' },
  { label: 'Tanks', detail: 'Raising the vessels and tanks' },
  { label: 'Columns', detail: 'Columns, ladders and platforms' },
  { label: 'Piping', detail: 'Pipe racks, lines and pumps' },
  { label: 'Finishing', detail: 'Labels, legend and controls' },
]

/** Index of the stage under way; a stage is done when its piece of the site is finished. */
export function siteStage(progress: number) {
  if (progress === 0) return 0
  return tankPart(progress) < 1 ? 1 : columnPart(progress) < 1 ? 2 : pipingPart(progress) < 1 ? 3 : 4
}
