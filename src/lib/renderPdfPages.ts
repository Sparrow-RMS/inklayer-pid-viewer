import * as pdfjsLib from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl

export interface RenderedPage {
  pageIndex: number
  width: number
  height: number
  /** PNG data URL */
  image: string
  /** Page size in PDF user-space points (scale-1 viewport) — needed to map
   *  normalized image-fraction bounding boxes back to PDF coordinates. */
  pdfWidth: number
  pdfHeight: number
}

/**
 * Renders every page of a PDF to a PNG data URL, capped at maxDimension on
 * the longer side. This is a separate pdfjs-dist instance from the one
 * InkLayer uses internally — kept independent since InkLayer doesn't expose
 * its own rendered pages for reuse.
 */
export async function renderPdfPagesToImages(data: ArrayBuffer, maxDimension = 2000): Promise<RenderedPage[]> {
  const doc = await pdfjsLib.getDocument({ data: data.slice(0) }).promise
  const pages: RenderedPage[] = []

  for (let pageNumber = 1; pageNumber <= doc.numPages; pageNumber++) {
    const page = await doc.getPage(pageNumber)
    const baseViewport = page.getViewport({ scale: 1 })
    const scale = maxDimension / Math.max(baseViewport.width, baseViewport.height)
    const viewport = page.getViewport({ scale })

    const canvas = document.createElement('canvas')
    canvas.width = Math.ceil(viewport.width)
    canvas.height = Math.ceil(viewport.height)
    const context = canvas.getContext('2d')
    if (!context) throw new Error('Could not create canvas context for PDF rendering')

    await page.render({ canvasContext: context, viewport }).promise

    pages.push({
      pageIndex: pageNumber - 1,
      width: canvas.width,
      height: canvas.height,
      image: canvas.toDataURL('image/png'),
      pdfWidth: baseViewport.width,
      pdfHeight: baseViewport.height,
    })
  }

  return pages
}
