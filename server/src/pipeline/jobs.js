/**
 * Job artifacts on disk.
 *
 * Each pipeline stage writes its output as JSON under a per-document job
 * directory, so a later stage can be re-run — and re-tuned — without paying
 * for the earlier ones again. Step 1 is by far the most expensive stage, and
 * steps 2 and 3 are the ones that need iterating on, so this caching is the
 * difference between a tight loop and a slow one.
 *
 * The job id is derived from the PDF's CONTENT, not the time or the file
 * name: re-running the same drawing lands in the same directory and reuses
 * what is already there, while an edited drawing gets a fresh one.
 */
import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile, readdir, stat } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { basename, extname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const JOBS_ROOT = process.env.PIPELINE_JOBS_DIR || fileURLToPath(new URL('../../.jobs/', import.meta.url))

const slug = (name) =>
  basename(name, extname(name))
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 40) || 'document'

export function jobIdFor(fileName, bytes) {
  const hash = createHash('sha256').update(bytes).digest('hex').slice(0, 12)
  return `${slug(fileName)}-${hash}`
}

export const jobDir = (jobId) => join(JOBS_ROOT, jobId)

export async function ensureJob(jobId, meta = {}) {
  const dir = jobDir(jobId)
  await mkdir(dir, { recursive: true })
  const manifestPath = join(dir, 'job.json')
  const existing = existsSync(manifestPath) ? JSON.parse(await readFile(manifestPath, 'utf8')) : {}
  const manifest = { jobId, createdAt: existing.createdAt ?? new Date().toISOString(), ...existing, ...meta, updatedAt: new Date().toISOString() }
  await writeFile(manifestPath, JSON.stringify(manifest, null, 2))
  return dir
}

export const artifactPath = (jobId, name) => join(jobDir(jobId), `${name}.json`)

export const hasArtifact = (jobId, name) => existsSync(artifactPath(jobId, name))

export async function writeArtifact(jobId, name, data) {
  await mkdir(jobDir(jobId), { recursive: true })
  const path = artifactPath(jobId, name)
  await writeFile(path, JSON.stringify(data, null, 2))
  return path
}

export async function readArtifact(jobId, name) {
  return JSON.parse(await readFile(artifactPath(jobId, name), 'utf8'))
}

/** Keeps the source PDF beside its artifacts so a re-run needs only the job id. */
export async function stashSource(jobId, fileName, bytes) {
  const dir = jobDir(jobId)
  await mkdir(dir, { recursive: true })
  const path = join(dir, 'source.pdf')
  if (!existsSync(path)) await writeFile(path, Buffer.from(bytes))
  return path
}

export const sourcePath = (jobId) => join(jobDir(jobId), 'source.pdf')

export async function listJobs() {
  if (!existsSync(JOBS_ROOT)) return []
  const entries = await readdir(JOBS_ROOT, { withFileTypes: true })
  const jobs = []
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    const dir = join(JOBS_ROOT, entry.name)
    const manifestPath = join(dir, 'job.json')
    const manifest = existsSync(manifestPath) ? JSON.parse(await readFile(manifestPath, 'utf8')) : { jobId: entry.name }
    const files = await readdir(dir)
    jobs.push({
      ...manifest,
      artifacts: files.filter((f) => f.endsWith('.json') && f !== 'job.json').map((f) => f.replace(/\.json$/, '')),
      updatedAt: manifest.updatedAt ?? (await stat(dir)).mtime.toISOString(),
    })
  }
  return jobs.sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))
}
