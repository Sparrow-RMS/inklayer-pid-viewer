/**
 * Client for the agent server's study-package routes
 * (agent/src/hazop_agent/study_package.py): the documents a HAZOP team asks
 * for before it starts, attached to the open drawing, and the readiness
 * checklist built from them.
 */
const HAZOP_API = import.meta.env.VITE_HAZOP_API_URL || '/agent/hazop'

export type DocumentStatus = 'provided' | 'partial' | 'missing'

export interface DocumentType {
  id: string
  name: string
  /** Why a HAZOP team needs it. */
  why: string
}

export interface StudyDocument {
  id: string
  studyId: string
  docType: string
  fileName: string
  contentType: string
  size: number
  note: string
  uploadedAt: string
}

export interface ChecklistItem extends DocumentType {
  status: DocumentStatus
  /** What the drawing itself already shows of this document, if anything. */
  fromDrawing: string | null
  documentIds: string[]
}

export interface StudyPackage {
  studyId: string
  documentTypes: DocumentType[]
  documents: StudyDocument[]
  checklist: {
    items: ChecklistItem[]
    provided: number
    partial: number
    missing: string[]
    total: number
    ready: boolean
  }
}

/** File types the server accepts, for the file picker. */
export const ACCEPTED_FILES = '.pdf,.xlsx,.xls,.xlsm,.csv,.docx,.doc,.txt,.png,.jpg,.jpeg,.dwg,.dxf'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${HAZOP_API}${path}`, init)
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = typeof body?.detail === 'string' ? body.detail : body?.detail?.[0]?.msg
    throw new Error(detail || `Study package request failed (${response.status})`)
  }
  return (response.status === 204 ? undefined : await response.json()) as T
}

export function getPackage(studyId: string): Promise<StudyPackage> {
  return request(`/studies/${encodeURIComponent(studyId)}/package`)
}

export function uploadDocument(studyId: string, docType: string, file: File): Promise<StudyDocument> {
  const params = new URLSearchParams({ docType, fileName: file.name })
  return request(`/studies/${encodeURIComponent(studyId)}/documents?${params}`, {
    method: 'POST',
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
  })
}

export function updateDocument(id: string, fields: { docType?: string; note?: string }): Promise<StudyDocument> {
  return request(`/documents/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  })
}

export function deleteDocument(id: string): Promise<void> {
  return request(`/documents/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function documentUrl(id: string): string {
  return `${HAZOP_API}/documents/${encodeURIComponent(id)}/file`
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
