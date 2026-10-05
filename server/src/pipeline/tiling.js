/**
 * Tile planning for exhaustive extraction.
 *
 * A P&ID page handed to a VLM whole loses its small content — instrument
 * bubbles, valve tags, line numbers are only a few pixels tall once an A1
 * sheet is squeezed into one image. So each page is cut into overlapping
 * tiles, each rendered at high resolution and read separately.
 *
 * Tiles OVERLAP on purpose: an entity straddling a cut would otherwise be
 * seen only in halves and described wrongly by both neighbours. The overlap
 * means anything smaller than the margin is fully inside at least one tile.
 * The duplicates this creates are the extractor's problem to merge, which is
 * the cheaper trade.
 */

/** Tiles are sized so each one covers roughly this fraction of the page's
 *  longest side; the grid is derived from the page's own aspect ratio so a
 *  wide A0 sheet gets more columns than rows rather than square-ish tiles. */
const DEFAULT_TILE_SPAN = 1 / 3

export function planTiles(pageGeom, options = {}) {
  const { overlap = 0.12, tileSpan = DEFAULT_TILE_SPAN } = options
  const aspect = pageGeom.width / pageGeom.height

  // Aim for tiles of similar on-page area whichever way the sheet is turned.
  let cols = options.cols ?? Math.max(1, Math.round((aspect >= 1 ? 1 : aspect) / tileSpan))
  let rows = options.rows ?? Math.max(1, Math.round((aspect >= 1 ? 1 / aspect : 1) / tileSpan))
  cols = Math.max(1, Math.min(8, cols))
  rows = Math.max(1, Math.min(8, rows))

  // Step between tile origins; each tile is one step wide plus the overlap
  // margin, so neighbours share `overlap` of a step on every shared edge.
  const stepX = 1 / cols
  const stepY = 1 / rows
  const padX = stepX * overlap
  const padY = stepY * overlap

  const tiles = []
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < cols; col++) {
      const x = Math.max(0, col * stepX - padX)
      const y = Math.max(0, row * stepY - padY)
      const right = Math.min(1, (col + 1) * stepX + padX)
      const bottom = Math.min(1, (row + 1) * stepY + padY)
      tiles.push({
        id: `p${pageGeom.pageIndex}-tile-${row}-${col}`,
        page: pageGeom.pageIndex,
        row,
        col,
        // Region of the page this tile shows, normalized page fractions.
        region: { x, y, width: right - x, height: bottom - y },
      })
    }
  }

  return { cols, rows, overlap, tiles }
}

/** Page-normalized bbox -> tile-relative (0..1 within the tile). */
export function bboxToTile(bbox, tile) {
  const { region } = tile
  return {
    x: (bbox.x - region.x) / region.width,
    y: (bbox.y - region.y) / region.height,
    width: bbox.width / region.width,
    height: bbox.height / region.height,
  }
}

/** Tile-relative bbox -> page-normalized. The inverse of bboxToTile. */
export function bboxFromTile(bbox, tile) {
  const { region } = tile
  return {
    x: region.x + bbox.x * region.width,
    y: region.y + bbox.y * region.height,
    width: bbox.width * region.width,
    height: bbox.height * region.height,
  }
}

export function pointFromTile(point, tile) {
  const { region } = tile
  return { x: region.x + point.x * region.width, y: region.y + point.y * region.height }
}

/** Text items visible in this tile. `minContainment` keeps a label that is
 *  mostly inside; one clipped at the very edge belongs to the neighbour. */
export function textItemsInTile(textItems, tile, minContainment = 0.5) {
  const { region } = tile
  const rx1 = region.x + region.width
  const ry1 = region.y + region.height
  return textItems.filter((item) => {
    const b = item.bbox
    const w = Math.min(b.x + b.width, rx1) - Math.max(b.x, region.x)
    const h = Math.min(b.y + b.height, ry1) - Math.max(b.y, region.y)
    if (w <= 0 || h <= 0) return false
    const area = Math.max(b.width * b.height, 1e-9)
    return (w * h) / area >= minContainment
  })
}
