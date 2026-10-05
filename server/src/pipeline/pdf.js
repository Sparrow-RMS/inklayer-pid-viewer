/**
 * Node-side PDF access for the pipeline: page rasterization plus the two
 * exact-geometry sources a vector (CAD-exported) P&ID gives us for free —
 * the text layer and the stroked vector paths.
 *
 * Everything returned here is in NORMALIZED, TOP-LEFT-ORIGIN page fractions
 * (see geometry.js), so nothing downstream has to know about PDF user space.
 */
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { bboxFromPoints } from './geometry.js'

const require = createRequire(import.meta.url)
const canvasLib = require('@napi-rs/canvas')

// pdfjs' legacy build reads these off globalThis when it evaluates, so they
// have to be installed BEFORE the dynamic import below.
for (const key of ['DOMMatrix', 'Path2D', 'ImageData', 'DOMPoint', 'DOMRect']) {
  if (!(key in globalThis)) globalThis[key] = canvasLib[key]
}

const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs')
const { OPS, Util } = pdfjs

// Base-14 fonts (Helvetica et al) aren't embedded in most PDFs. Without this
// pointing at a real directory path — a filesystem path, NOT a file:// URL —
// pdfjs renders every glyph as nothing and only warns. `useSystemFonts` must
// stay false: in Node it takes precedence and then fails the same way.
const STANDARD_FONTS = fileURLToPath(new URL('../../node_modules/pdfjs-dist/standard_fonts/', import.meta.url))
const CMAPS = fileURLToPath(new URL('../../node_modules/pdfjs-dist/cmaps/', import.meta.url))

export async function loadPdf(source) {
  const bytes = typeof source === 'string' ? new Uint8Array(await readFile(source)) : new Uint8Array(source)
  return pdfjs.getDocument({
    data: bytes,
    standardFontDataUrl: STANDARD_FONTS,
    cMapUrl: CMAPS,
    cMapPacked: true,
    useSystemFonts: false,
  }).promise
}

/** Scale-1 viewport size, with any /Rotate already applied. */
export function pageGeometry(page, pageIndex) {
  const viewport = page.getViewport({ scale: 1 })
  return { pageIndex, width: viewport.width, height: viewport.height, rotation: page.rotate ?? 0 }
}

/**
 * Every text run on the page with its exact box.
 *
 * pdfjs hands back a text-space matrix per item plus `width`/`height` in PDF
 * user-space units. Rather than reasoning about the y-flip and page rotation
 * by hand, this walks the item's four corners through the page viewport's own
 * transform — which handles both — and takes the axis-aligned bounds. That
 * keeps rotated tags (vertical line labels are common on P&IDs) correct
 * instead of collapsing them to a zero-width box.
 */
export async function extractTextItems(page, pageIndex) {
  const viewport = page.getViewport({ scale: 1 })
  const { items } = await page.getTextContent({ includeMarkedContent: false })
  const out = []

  items.forEach((item, i) => {
    const str = item.str
    if (!str || !str.trim()) return

    const [a, b, c, d, e, f] = item.transform
    const advanceLen = Math.hypot(a, b) || 1
    const upLen = Math.hypot(c, d) || 1
    const advance = { x: (a / advanceLen) * item.width, y: (b / advanceLen) * item.width }
    const up = { x: (c / upLen) * item.height, y: (d / upLen) * item.height }

    const corners = [
      [e, f],
      [e + advance.x, f + advance.y],
      [e + up.x, f + up.y],
      [e + advance.x + up.x, f + advance.y + up.y],
    ].map(([x, y]) => {
      const [vx, vy] = Util.applyTransform([x, y], viewport.transform)
      return { x: vx / viewport.width, y: vy / viewport.height }
    })

    const bbox = bboxFromPoints(corners)
    if (!bbox) return

    // Rotation of the text run in viewport space, degrees clockwise from
    // left-to-right. Kept so step 2 can tell a vertical line label apart
    // from a horizontal equipment tag.
    const [ox, oy] = Util.applyTransform([e, f], viewport.transform)
    const [ax, ay] = Util.applyTransform([e + advance.x, f + advance.y], viewport.transform)
    const angle = Math.round((Math.atan2(ay - oy, ax - ox) * 180) / Math.PI)

    out.push({
      id: `p${pageIndex}-t${i}`,
      page: pageIndex,
      str: str.trim(),
      bbox,
      fontSize: item.height,
      angle,
      fontName: item.fontName ?? null,
    })
  })

  return out
}

/**
 * Stroked straight-line segments from the page's vector content.
 *
 * On a CAD-exported P&ID this is the actual drawn geometry — pipe runs,
 * symbol outlines, leader lines, border rules — at full precision. Step 3
 * uses it to decide whether two processes are genuinely connected by pipe
 * rather than merely near each other on the page.
 *
 * Only `stroke`d subpaths are kept (a filled shape is a symbol body, not a
 * pipe), curves are reduced to their endpoints, and `rectangle` ops are
 * emitted as their four sides. Segments are tagged with the stroke width so
 * a consumer can separate heavy process lines from light instrument leaders.
 */
export async function extractVectorSegments(page, pageIndex, { minLength = 0.002 } = {}) {
  const viewport = page.getViewport({ scale: 1 })
  const opList = await page.getOperatorList()

  let ctm = viewport.transform
  let lineWidth = 1
  let dashed = false
  const ctmStack = []
  const widthStack = []
  const segments = []

  const toNorm = (pt) => {
    const [x, y] = Util.applyTransform(pt, ctm)
    return { x: x / viewport.width, y: y / viewport.height }
  }

  // Pending subpath geometry, flushed only if the path is actually stroked.
  let pending = []

  const flush = (stroked) => {
    if (stroked) {
      for (const seg of pending) {
        const len = Math.hypot(seg.b.x - seg.a.x, seg.b.y - seg.a.y)
        if (len >= minLength) segments.push({ ...seg, length: len })
      }
    }
    pending = []
  }

  for (let i = 0; i < opList.fnArray.length; i++) {
    const fn = opList.fnArray[i]
    const args = opList.argsArray[i]

    if (fn === OPS.save) {
      ctmStack.push(ctm)
      widthStack.push([lineWidth, dashed])
    } else if (fn === OPS.restore) {
      ctm = ctmStack.pop() ?? ctm
      ;[lineWidth, dashed] = widthStack.pop() ?? [lineWidth, dashed]
    } else if (fn === OPS.transform) {
      ctm = Util.transform(ctm, args)
    } else if (fn === OPS.setLineWidth) {
      lineWidth = args[0]
    } else if (fn === OPS.setDash) {
      dashed = Array.isArray(args[0]) && args[0].length > 0
    } else if (fn === OPS.constructPath) {
      const [pathOps, coords] = args
      let c = 0
      let cur = null
      let start = null
      for (const op of pathOps) {
        if (op === OPS.moveTo) {
          cur = [coords[c++], coords[c++]]
          start = cur
        } else if (op === OPS.lineTo) {
          const next = [coords[c++], coords[c++]]
          if (cur) pending.push({ a: toNorm(cur), b: toNorm(next), width: lineWidth, dashed })
          cur = next
        } else if (op === OPS.curveTo) {
          // Approximate a bezier by its chord: pipe routing on a P&ID is
          // overwhelmingly orthogonal, so curves here are almost always
          // symbol detail we only need the extent of.
          c += 4
          const next = [coords[c++], coords[c++]]
          if (cur) pending.push({ a: toNorm(cur), b: toNorm(next), width: lineWidth, dashed, approx: true })
          cur = next
        } else if (op === OPS.closePath) {
          if (cur && start) pending.push({ a: toNorm(cur), b: toNorm(start), width: lineWidth, dashed })
          cur = start
        } else if (op === OPS.rectangle) {
          const [x, y, w, h] = [coords[c++], coords[c++], coords[c++], coords[c++]]
          const pts = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]].map(toNorm)
          for (let k = 0; k < 4; k++) {
            pending.push({ a: pts[k], b: pts[(k + 1) % 4], width: lineWidth, dashed })
          }
          cur = [x, y]
          start = cur
        }
      }
    } else if (fn === OPS.stroke || fn === OPS.closeStroke) {
      flush(true)
    } else if (fn === OPS.fill || fn === OPS.eoFill) {
      flush(false)
    } else if (fn === OPS.fillStroke || fn === OPS.eoFillStroke || fn === OPS.closeFillStroke || fn === OPS.closeEOFillStroke) {
      flush(true)
    } else if (fn === OPS.endPath) {
      flush(false)
    }
  }
  flush(false)

  return segments.map((s, i) => ({ id: `p${pageIndex}-s${i}`, page: pageIndex, ...s }))
}

/**
 * Rasterizes an axis-aligned region of the page, given in normalized page
 * fractions, to a PNG buffer. `targetPx` caps the longest rendered side —
 * the region is scaled up as far as that allows, which is what makes small
 * instrument bubbles legible inside a tile.
 */
export async function renderRegion(page, region, { targetPx = 1600, background = '#ffffff' } = {}) {
  const base = page.getViewport({ scale: 1 })
  const x0 = region.x * base.width
  const y0 = region.y * base.height
  const regionW = region.width * base.width
  const regionH = region.height * base.height

  const scale = targetPx / Math.max(regionW, regionH)
  const width = Math.max(1, Math.round(regionW * scale))
  const height = Math.max(1, Math.round(regionH * scale))

  const canvas = canvasLib.createCanvas(width, height)
  const context = canvas.getContext('2d')
  context.fillStyle = background
  context.fillRect(0, 0, width, height)

  // offsetX/offsetY shift the page inside the viewport, which is how a crop
  // is expressed: push the region's top-left corner back to the origin.
  const viewport = page.getViewport({ scale, offsetX: -x0 * scale, offsetY: -y0 * scale })
  await page.render({ canvasContext: context, viewport }).promise

  return { buffer: canvas.toBuffer('image/png'), width, height, scale }
}

export const toDataUrl = (buffer) => `data:image/png;base64,${buffer.toString('base64')}`
