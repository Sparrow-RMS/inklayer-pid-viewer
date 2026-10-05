/**
 * Geometry helpers for the P&ID pipeline.
 *
 * Coordinate convention used EVERYWHERE downstream of `pdf.js`:
 *   - normalized fractions (0..1) of the page's scale-1 viewport
 *   - top-left origin, y growing downward
 * This is the same space `src/lib/annotationFromNode.ts` already consumes on
 * the frontend, so step 4 needs no further conversion.
 *
 * A `BBox` is { x, y, width, height }; a `Point` is { x, y }.
 */

export const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v)

export function clampBbox(b) {
  const x = clamp01(b.x)
  const y = clamp01(b.y)
  return { x, y, width: clamp01(b.x + b.width) - x, height: clamp01(b.y + b.height) - y }
}

export function bboxFromPoints(points) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const p of points) {
    if (p.x < minX) minX = p.x
    if (p.y < minY) minY = p.y
    if (p.x > maxX) maxX = p.x
    if (p.y > maxY) maxY = p.y
  }
  if (!Number.isFinite(minX)) return null
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY }
}

export function unionBbox(a, b) {
  if (!a) return b
  if (!b) return a
  const x = Math.min(a.x, b.x)
  const y = Math.min(a.y, b.y)
  return { x, y, width: Math.max(a.x + a.width, b.x + b.width) - x, height: Math.max(a.y + a.height, b.y + b.height) - y }
}

export const unionBboxes = (list) => list.reduce((acc, b) => unionBbox(acc, b), null)

export const bboxArea = (b) => (b ? Math.max(0, b.width) * Math.max(0, b.height) : 0)

export function intersectionArea(a, b) {
  if (!a || !b) return 0
  const w = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)
  const h = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y)
  return w > 0 && h > 0 ? w * h : 0
}

export function iou(a, b) {
  const inter = intersectionArea(a, b)
  if (inter === 0) return 0
  return inter / (bboxArea(a) + bboxArea(b) - inter)
}

/** How much of `inner` lies inside `outer` (0..1). Better than IoU when the
 *  two boxes are very different sizes — e.g. a tag label inside an equipment
 *  outline, where IoU stays near zero however well-contained it is. */
export function containment(inner, outer) {
  const a = bboxArea(inner)
  return a === 0 ? 0 : intersectionArea(inner, outer) / a
}

export const bboxCenter = (b) => ({ x: b.x + b.width / 2, y: b.y + b.height / 2 })

/** Edge-to-edge gap between two boxes; 0 when they touch or overlap. */
export function bboxGap(a, b) {
  const dx = Math.max(0, Math.max(a.x, b.x) - Math.min(a.x + a.width, b.x + b.width))
  const dy = Math.max(0, Math.max(a.y, b.y) - Math.min(a.y + a.height, b.y + b.height))
  return Math.hypot(dx, dy)
}

export const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y)

export function pointInBbox(p, b, pad = 0) {
  return p.x >= b.x - pad && p.x <= b.x + b.width + pad && p.y >= b.y - pad && p.y <= b.y + b.height + pad
}

/** Shortest distance from point `p` to segment `a`-`b`. */
export function pointSegmentDistance(p, a, b) {
  const vx = b.x - a.x, vy = b.y - a.y
  const len2 = vx * vx + vy * vy
  if (len2 === 0) return distance(p, a)
  let t = ((p.x - a.x) * vx + (p.y - a.y) * vy) / len2
  t = t < 0 ? 0 : t > 1 ? 1 : t
  return Math.hypot(p.x - (a.x + t * vx), p.y - (a.y + t * vy))
}

export function polylineLength(points) {
  let total = 0
  for (let i = 1; i < points.length; i++) total += distance(points[i - 1], points[i])
  return total
}
