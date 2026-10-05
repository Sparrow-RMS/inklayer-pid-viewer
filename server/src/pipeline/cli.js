#!/usr/bin/env node
/**
 * Pipeline CLI — run a stage against a PDF and inspect what came out.
 *
 *   node src/pipeline/cli.js step1 drawing.pdf
 *   node src/pipeline/cli.js inspect <jobId>
 *   node src/pipeline/cli.js list
 *
 * Artifacts land in server/.jobs/<jobId>/ and are reused on a re-run unless
 * --force is passed, so tuning a later stage never re-pays for step 1.
 */
import 'dotenv/config'
import { readFile } from 'node:fs/promises'
import { basename } from 'node:path'
import { loadPdf } from './pdf.js'
import { extractDocument, assembleDocument } from './step1/extract.js'
import { classifyDocument, assembleDocument as assembleStep2Document } from './step2/classify.js'
import { clusterDocument, assembleDocument as assembleStep3Document } from './step3/cluster.js'
import { consolidateDocument, assembleDocument as assembleConsolidateDocument } from './step3/consolidate.js'
import { jobIdFor, ensureJob, writeArtifact, readArtifact, hasArtifact, stashSource, sourcePath, listJobs, jobDir } from './jobs.js'

function parseArgs(argv) {
  const positional = []
  const flags = {}
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]
    if (arg.startsWith('--')) {
      const [key, inline] = arg.slice(2).split('=')
      const camel = key.replace(/-([a-z])/g, (_, c) => c.toUpperCase())
      if (inline !== undefined) flags[camel] = inline
      else if (argv[i + 1] && !argv[i + 1].startsWith('--')) flags[camel] = argv[++i]
      else flags[camel] = true
    } else positional.push(arg)
  }
  return { positional, flags }
}

const num = (v, fallback) => (v === undefined ? fallback : Number(v))

async function cmdStep1(target, flags) {
  if (!target) throw new Error('usage: cli.js step1 <file.pdf> [--pages 0,2] [--cols 3] [--rows 2] [--force]')
  if (!process.env.OPENAI_API_KEY) throw new Error('OPENAI_API_KEY is not set (put it in server/.env)')

  const bytes = await readFile(target)
  const fileName = basename(target)
  const jobId = jobIdFor(fileName, bytes)

  if (hasArtifact(jobId, 'step1') && !flags.force) {
    const existing = await readArtifact(jobId, 'step1')
    console.log(`step1 already done for job ${jobId} (${existing.totals.entities} entities). Re-run with --force to redo it.`)
    return summarizeStep1(existing, jobId)
  }

  await ensureJob(jobId, { fileName, source: target })
  await stashSource(jobId, fileName, bytes)

  const doc = await loadPdf(new Uint8Array(bytes))
  console.log(`[step1] ${fileName} — ${doc.numPages} page(s), job ${jobId}`)

  const started = Date.now()
  const result = await extractDocument(doc, {
    fileName,
    pageIndexes: flags.pages ? String(flags.pages).split(',').map(Number) : null,
    concurrency: num(flags.concurrency, 4),
    tilePx: num(flags.tilePx, 1600),
    pagePx: num(flags.pagePx, 2200),
    includeVectors: flags.noVectors !== true,
    tiling: {
      cols: flags.cols ? Number(flags.cols) : undefined,
      rows: flags.rows ? Number(flags.rows) : undefined,
      overlap: num(flags.overlap, 0.12),
    },
    onProgress: (event) => {
      if (event.phase === 'page-start') {
        console.log(`  page ${event.pageIndex}: ${event.tiles} tiles, ${event.textItems} text items, ${event.vectorSegments} vector segments`)
      } else if (event.phase === 'tile-done') {
        process.stdout.write(`    ${event.tile}: ${event.entities} entities\n`)
      } else if (event.phase?.endsWith('failed')) {
        console.warn(`    ! ${event.phase} ${event.tile ?? ''} — ${event.error}`)
      }
    },
  })

  result.durationMs = Date.now() - started

  // The raw tile responses are cached separately so `remerge` can replay the
  // deterministic half — merging, anchoring, de-duplication — without paying
  // for another pass over the sheet.
  const { raw, ...clean } = result
  await writeArtifact(jobId, 'step1-raw', { fileName, pageCount: doc.numPages, pages: raw })
  const path = await writeArtifact(jobId, 'step1', clean)
  await ensureJob(jobId, { lastStep: 'step1' })
  console.log(`\n[step1] wrote ${path} in ${(result.durationMs / 1000).toFixed(1)}s`)
  summarizeStep1(clean, jobId)
}

function summarizeStep1(result, jobId) {
  console.log(`\nJob ${jobId} — ${result.fileName}`)
  for (const page of result.pages) {
    const byCategory = {}
    let anchored = 0
    for (const e of page.entities) {
      byCategory[e.category] = (byCategory[e.category] ?? 0) + 1
      if (e.anchored) anchored++
    }
    const pct = page.entities.length ? Math.round((anchored / page.entities.length) * 100) : 0
    console.log(`\n  page ${page.pageIndex}  (${page.tiling.cols}x${page.tiling.rows} tiles)`)
    console.log(`    entities: ${page.entities.length}  (${pct}% anchored to exact PDF text)`)
    for (const [category, n] of Object.entries(byCategory).sort((a, b) => b[1] - a[1])) {
      console.log(`      ${category.padEnd(20)} ${n}`)
    }
    const flagged = page.entities.filter((e) => e.flags.tagMismatch || e.flags.conflictingTypes)
    if (flagged.length) console.log(`    flagged for review: ${flagged.length}`)
    if (page.diagnostics.failedTiles.length) console.log(`    FAILED TILES: ${page.diagnostics.failedTiles.length}`)
  }
  console.log(`\n  artifacts: ${jobDir(jobId)}`)
}

/** Replays only the deterministic half of step 1 over the cached responses. */
async function cmdRemerge(jobId) {
  if (!jobId) throw new Error('usage: cli.js remerge <jobId>')
  if (!hasArtifact(jobId, 'step1-raw')) {
    throw new Error(`no cached raw responses for ${jobId} — run step1 first (jobs created before caching won't have them)`)
  }
  const cached = await readArtifact(jobId, 'step1-raw')
  const started = Date.now()
  const result = assembleDocument(cached.pages, cached.fileName, cached.pageCount)
  result.durationMs = Date.now() - started
  await writeArtifact(jobId, 'step1', result)
  console.log(`[remerge] reassembled from cache in ${result.durationMs}ms — no model calls`)
  summarizeStep1(result, jobId)
}

async function cmdStep2(jobId, flags) {
  if (!jobId) throw new Error('usage: cli.js step2 <jobId> [--force]')
  if (!process.env.OPENAI_API_KEY) throw new Error('OPENAI_API_KEY is not set (put it in server/.env)')
  if (!hasArtifact(jobId, 'step1')) throw new Error(`no step1 artifact for ${jobId} — run step1 first`)

  if (hasArtifact(jobId, 'step2') && !flags.force) {
    const existing = await readArtifact(jobId, 'step2')
    console.log(`step2 already done for job ${jobId} (${existing.totals.processes} processes). Re-run with --force to redo it.`)
    return summarizeStep2(existing, jobId)
  }

  const step1Result = await readArtifact(jobId, 'step1')
  const doc = await loadPdf(sourcePath(jobId))
  console.log(`[step2] ${step1Result.fileName} — job ${jobId}`)

  const started = Date.now()
  const result = await classifyDocument(doc, step1Result, {
    pagePx: num(flags.pagePx, 2400),
    onProgress: (event) => {
      if (event.phase === 'page-start') console.log(`  page ${event.pageIndex}: classifying ${event.classifiableEntities} entities`)
      else if (event.phase === 'page-done') console.log(`    -> ${event.processes} processes proposed`)
      else if (event.phase === 'page-failed') console.warn(`    ! page ${event.pageIndex} failed: ${event.error}`)
    },
  })
  result.durationMs = Date.now() - started

  const { raw, ...clean } = result
  await writeArtifact(jobId, 'step2-raw', { fileName: step1Result.fileName, pages: raw })
  await writeArtifact(jobId, 'step2', clean)
  await ensureJob(jobId, { lastStep: 'step2' })
  console.log(`\n[step2] wrote step2.json in ${(result.durationMs / 1000).toFixed(1)}s`)
  summarizeStep2(clean, jobId)
}

function summarizeStep2(result, jobId) {
  console.log(`\nJob ${jobId} — ${result.fileName}`)
  for (const page of result.pages) {
    const byCategory = {}
    for (const p of page.processes) byCategory[p.category] = (byCategory[p.category] ?? 0) + 1
    const { classifiableEntities, coveredEntityIds, uncoveredEntityIds } = page.diagnostics
    const pct = classifiableEntities ? Math.round((coveredEntityIds.length / classifiableEntities) * 100) : 0
    console.log(`\n  page ${page.pageIndex}`)
    console.log(`    processes: ${page.processes.length}`)
    for (const [category, n] of Object.entries(byCategory).sort((a, b) => b[1] - a[1])) {
      console.log(`      ${category.padEnd(16)} ${n}`)
    }
    console.log(`    entity coverage: ${coveredEntityIds.length}/${classifiableEntities} (${pct}%)`)
    if (uncoveredEntityIds.length) console.log(`    uncovered: ${uncoveredEntityIds.length} — see diagnostics.uncoveredEntityIds`)
    const hallucinated = page.processes.filter((p) => p.flags.hallucinatedIds.length)
    if (hallucinated.length) console.log(`    processes with a hallucinated id (dropped, not trusted): ${hallucinated.length}`)
    const overlapping = page.processes.filter((p) => p.flags.highOverlapWith?.length)
    if (overlapping.length) console.log(`    high-overlap pairs flagged for review: ${overlapping.length / 2}`)
    if (page.diagnostics.pageNotes) console.log(`    model notes: ${page.diagnostics.pageNotes}`)
  }
  console.log(`\n  artifacts: ${jobDir(jobId)}`)
}

/** Replays only the deterministic half of step 2 over cached responses. */
async function cmdRemerge2(jobId) {
  if (!jobId) throw new Error('usage: cli.js remerge2 <jobId>')
  if (!hasArtifact(jobId, 'step2-raw')) throw new Error(`no cached raw responses for ${jobId} — run step2 first`)
  const cached = await readArtifact(jobId, 'step2-raw')
  const started = Date.now()
  const result = assembleStep2Document(cached.pages, cached.fileName)
  result.durationMs = Date.now() - started
  await writeArtifact(jobId, 'step2', result)
  console.log(`[remerge2] reassembled from cache in ${result.durationMs}ms — no model calls`)
  summarizeStep2(result, jobId)
}

async function cmdProcesses(jobId, flags) {
  if (!jobId) throw new Error('usage: cli.js processes <jobId> [--page 0] [--category transfer]')
  const result = await readArtifact(jobId, 'step2')
  const step1 = await readArtifact(jobId, 'step1')
  for (const page of result.pages) {
    if (flags.page !== undefined && page.pageIndex !== Number(flags.page)) continue
    const entityById = new Map(step1.pages.find((p) => p.pageIndex === page.pageIndex).entities.map((e) => [e.id, e]))
    const rows = page.processes.filter((p) => !flags.category || p.category === flags.category)
    console.log(`\npage ${page.pageIndex} — ${rows.length} processes`)
    for (const p of rows) {
      console.log(`\n  ${p.id}  [${p.category}]  ${p.name}  (confidence ${p.confidence})`)
      console.log(`    ${p.description}`)
      if (p.flowDirection) console.log(`    flow: ${p.flowDirection}`)
      const tags = p.entityIds.map((id) => entityById.get(id)?.tag ?? id).join(', ')
      console.log(`    entities (${p.entityIds.length}): ${tags}`)
      if (p.flags.hallucinatedIds.length) console.log(`    ! hallucinated ids dropped: ${p.flags.hallucinatedIds.join(', ')}`)
      if (p.flags.highOverlapWith?.length) console.log(`    ! high overlap with: ${p.flags.highOverlapWith.join(', ')}`)
    }
  }
}

async function cmdStep3(jobId, flags) {
  if (!jobId) throw new Error('usage: cli.js step3 <jobId> [--force] [--max-nodes 10]')
  if (!process.env.OPENAI_API_KEY) throw new Error('OPENAI_API_KEY is not set (put it in server/.env)')
  if (!hasArtifact(jobId, 'step2')) throw new Error(`no step2 artifact for ${jobId} — run step2 first`)

  const targetMaxNodes = flags.maxNodes ? Number(flags.maxNodes) : undefined

  let clean
  if (hasArtifact(jobId, 'step3') && !flags.force) {
    clean = await readArtifact(jobId, 'step3')
    console.log(`step3 already done for job ${jobId} (${clean.totals.nodes} nodes). Re-run with --force to redo it.`)
  } else {
    const step1Result = await readArtifact(jobId, 'step1')
    const step2Result = await readArtifact(jobId, 'step2')
    console.log(`[step3] ${step2Result.fileName} — job ${jobId}`)

    const started = Date.now()
    const result = await clusterDocument(step2Result, step1Result, {
      maxPairsPerCall: num(flags.maxPairsPerCall, 40),
      concurrency: num(flags.concurrency, 3),
      onProgress: (event) => {
        if (event.phase === 'page-start') console.log(`  page ${event.pageIndex}: ${event.processes} processes -> ${event.candidatePairs} candidate pairs`)
        else if (event.phase === 'chunk-done') console.log(`    chunk ${event.chunk}: ${event.verdicts} verdicts`)
        else if (event.phase === 'chunk-failed') console.warn(`    ! chunk ${event.chunk} failed: ${event.error}`)
      },
    })
    result.durationMs = Date.now() - started

    const { raw, ...cleanResult } = result
    clean = cleanResult
    await writeArtifact(jobId, 'step3-raw', { fileName: step2Result.fileName, pages: raw })
    await writeArtifact(jobId, 'step3', clean)
    await ensureJob(jobId, { lastStep: 'step3' })
    console.log(`\n[step3] wrote step3.json in ${(result.durationMs / 1000).toFixed(1)}s`)
    summarizeStep3(clean, jobId)
  }

  if (targetMaxNodes) await runConsolidation(jobId, clean, targetMaxNodes, flags)
}

/** Runs the consolidation pass (only reachable via --max-nodes) on top of an
 *  already-computed base clustering, and writes it as its OWN artifact
 *  (step3-consolidated.json) rather than overwriting step3.json — the base,
 *  fine-grained clustering stays inspectable and untouched. */
async function runConsolidation(jobId, step3Result, targetMaxNodes, flags) {
  if (!process.env.OPENAI_API_KEY) throw new Error('OPENAI_API_KEY is not set (put it in server/.env)')

  if (hasArtifact(jobId, 'step3-consolidated') && !flags.force) {
    const existing = await readArtifact(jobId, 'step3-consolidated')
    if (existing.targetMaxNodes === targetMaxNodes) {
      console.log(`\nconsolidation to <= ${targetMaxNodes} nodes already done. Re-run with --force to redo it, or 'remerge3' to retarget for free.`)
      return summarizeConsolidation(existing, jobId)
    }
  }

  const step1Result = await readArtifact(jobId, 'step1')
  console.log(`\n[consolidate] targeting <= ${targetMaxNodes} nodes`)

  const started = Date.now()
  const result = await consolidateDocument(step3Result, step1Result, {
    targetMaxNodes,
    minRelatedness: num(flags.minRelatedness, 0.3),
    maxPairsPerCall: num(flags.maxPairsPerCall, 40),
    concurrency: num(flags.concurrency, 3),
    onProgress: (event) => {
      if (event.phase === 'consolidate-skip') console.log(`  page ${event.pageIndex}: already <= ${event.targetMaxNodes} nodes (${event.nodes}) — nothing to do`)
      else if (event.phase === 'consolidate-start') console.log(`  page ${event.pageIndex}: ${event.nodes} nodes -> scoring ${event.pairs} pairs`)
      else if (event.phase === 'consolidate-chunk-done') console.log(`    chunk ${event.chunk}: ${event.scores} scores`)
      else if (event.phase === 'consolidate-chunk-failed') console.warn(`    ! chunk ${event.chunk} failed: ${event.error}`)
    },
  })
  result.durationMs = Date.now() - started

  const { raw, ...clean } = result
  await writeArtifact(jobId, 'step3-consolidate-raw', { fileName: result.fileName, pages: raw })
  await writeArtifact(jobId, 'step3-consolidated', clean)
  await ensureJob(jobId, { lastStep: 'step3-consolidate' })
  console.log(`\n[consolidate] wrote step3-consolidated.json in ${(result.durationMs / 1000).toFixed(1)}s`)
  summarizeConsolidation(clean, jobId)
}

function summarizeConsolidation(result, jobId) {
  console.log(`\nJob ${jobId} — ${result.fileName} — consolidated to target <= ${result.targetMaxNodes}`)
  for (const page of result.pages) {
    const d = page.diagnostics
    if (d.skipped) {
      console.log(`\n  page ${page.pageIndex}: skipped — ${d.reason}`)
      continue
    }
    console.log(`\n  page ${page.pageIndex}`)
    console.log(`    ${d.startingNodes} nodes -> ${d.finalNodes} nodes  (target ${d.reachedTarget ? 'met' : 'NOT MET'})`)
    console.log(`    pairs scored: ${d.pairsScored}  merges applied: ${d.appliedMerges.length}  of which forced (below relatedness ${flagsFloorNote()}): ${d.forcedMergeCount}`)
    if (d.invalidPairIds.length) console.log(`    ! invalid pair ids referenced by the model: ${d.invalidPairIds.length}`)
    if (d.missingScores.length) console.log(`    ! candidate pairs never scored: ${d.missingScores.length}`)
    if (d.modelError) console.log(`    ! model error: ${d.modelError}`)
    if (d.forcedMergeCount > 0) {
      console.log(`    forced merges (kept the target, but these weren't genuinely related — review):`)
      for (const m of d.appliedMerges.filter((x) => x.forced)) {
        console.log(`      ${m.aId} + ${m.bId}  (relatedness ${m.relatedness.toFixed(2)}) — ${m.reasoning}`)
      }
    }
  }
  console.log(`\n  artifacts: ${jobDir(jobId)}`)
}
const flagsFloorNote = () => '0.3 by default, --min-relatedness to change'

function summarizeStep3(result, jobId) {
  console.log(`\nJob ${jobId} — ${result.fileName}`)
  for (const page of result.pages) {
    const byMemberCount = {}
    for (const n of page.nodes) byMemberCount[n.memberCount] = (byMemberCount[n.memberCount] ?? 0) + 1
    console.log(`\n  page ${page.pageIndex}`)
    console.log(`    ${page.processes.length} processes -> ${page.nodes.length} nodes`)
    for (const [size, n] of Object.entries(byMemberCount).sort((a, b) => a[0] - b[0])) {
      console.log(`      ${size}-process node${size === '1' ? ' ' : 's'}: ${n}`)
    }
    const d = page.diagnostics
    console.log(`    candidate pairs: ${d.candidatePairs}  verdicts: ${d.verdictsReceived}  applied (merged): ${d.verdictsApplied}`)
    if (d.invalidPairIds.length) console.log(`    ! invalid pair ids referenced by the model: ${d.invalidPairIds.length}`)
    if (d.missingVerdicts.length) console.log(`    ! candidate pairs never answered: ${d.missingVerdicts.length}`)
    if (d.modelError) console.log(`    ! model error: ${d.modelError}`)
  }
  console.log(`\n  artifacts: ${jobDir(jobId)}`)
}

/** Replays only the deterministic half of step 3 (DSU + geometry) over
 *  cached verdicts. */
async function cmdRemerge3(jobId, flags) {
  if (!jobId) throw new Error('usage: cli.js remerge3 <jobId> [--max-nodes 10]')
  if (!hasArtifact(jobId, 'step3-raw')) throw new Error(`no cached raw responses for ${jobId} — run step3 first`)
  const cached = await readArtifact(jobId, 'step3-raw')
  const started = Date.now()
  const result = assembleStep3Document(cached.pages, cached.fileName)
  result.durationMs = Date.now() - started
  await writeArtifact(jobId, 'step3', result)
  console.log(`[remerge3] reassembled base clustering from cache in ${result.durationMs}ms — no model calls`)
  summarizeStep3(result, jobId)

  const targetMaxNodes = flags.maxNodes ? Number(flags.maxNodes) : undefined
  if (!targetMaxNodes) return

  // The relatedness matrix doesn't depend on the target — only where the
  // greedy merge stops does — so retargeting (even to a number never tried
  // before) replays entirely from cache, no new model calls, as long as a
  // consolidation pass has been run at least once for this job.
  if (!hasArtifact(jobId, 'step3-consolidate-raw')) {
    console.log(`\nno cached consolidation scores yet for ${jobId} — run 'step3 ${jobId} --max-nodes ${targetMaxNodes}' once first (this needs a model call); after that, retargeting is free.`)
    return
  }
  const consolidateCached = await readArtifact(jobId, 'step3-consolidate-raw')
  const cStarted = Date.now()
  const consolidated = assembleConsolidateDocument(consolidateCached.pages, consolidateCached.fileName, {
    targetMaxNodes,
    minRelatedness: num(flags.minRelatedness, 0.3),
  })
  consolidated.durationMs = Date.now() - cStarted
  await writeArtifact(jobId, 'step3-consolidated', consolidated)
  console.log(`\n[remerge3] retargeted consolidation to <= ${targetMaxNodes} from cache in ${consolidated.durationMs}ms — no model calls`)
  summarizeConsolidation(consolidated, jobId)
}

async function cmdNodes(jobId, flags) {
  if (!jobId) throw new Error('usage: cli.js nodes <jobId> [--page 0] [--consolidated]')
  const result = await readArtifact(jobId, flags.consolidated ? 'step3-consolidated' : 'step3')
  const step1 = await readArtifact(jobId, 'step1')
  for (const page of result.pages) {
    if (flags.page !== undefined && page.pageIndex !== Number(flags.page)) continue
    const entityById = new Map(step1.pages.find((p) => p.pageIndex === page.pageIndex).entities.map((e) => [e.id, e]))
    console.log(`\npage ${page.pageIndex} — ${page.nodes.length} nodes`)
    for (const node of page.nodes) {
      console.log(`\n  ${node.id}  [${node.categories.join('/')}]  ${node.label}  (${node.memberCount} process${node.memberCount === 1 ? '' : 'es'})`)
      console.log(`    ${node.description}`)
      const tags = node.primaryEquipmentIds.map((id) => entityById.get(id)?.tag ?? id).join(', ')
      if (tags) console.log(`    primary equipment: ${tags}`)
      console.log(`    entities: ${node.entityIds.length}  routes: ${node.routes.length}`)
    }
  }
}

async function cmdInspect(jobId, flags) {
  if (!jobId) throw new Error('usage: cli.js inspect <jobId> [--category valve] [--page 0] [--limit 40]')
  const result = await readArtifact(jobId, flags.step ? `step${flags.step}` : 'step1')
  const limit = num(flags.limit, 40)
  for (const page of result.pages) {
    if (flags.page !== undefined && page.pageIndex !== Number(flags.page)) continue
    const rows = page.entities.filter((e) => !flags.category || e.category === flags.category)
    console.log(`\npage ${page.pageIndex} — ${rows.length} matching entities`)
    for (const e of rows.slice(0, limit)) {
      const at = `(${e.bbox.x.toFixed(3)}, ${e.bbox.y.toFixed(3)})`
      const mark = e.anchored ? '*' : ' '
      console.log(`  ${mark} ${e.id.padEnd(12)} ${e.category.padEnd(12)} ${String(e.tag ?? '—').padEnd(18)} ${at}  ${e.type}`)
      if (e.attributes.length) console.log(`      ${e.attributes.map((a) => `${a.key}=${a.value}`).join('  ')}`)
    }
    if (rows.length > limit) console.log(`  … ${rows.length - limit} more`)
  }
  console.log('\n(* = coordinates anchored to the PDF text layer, i.e. exact)')
}

async function cmdList() {
  const jobs = await listJobs()
  if (!jobs.length) return console.log('no jobs yet')
  for (const job of jobs) {
    console.log(`${job.jobId.padEnd(56)} ${(job.artifacts.join(',') || '—').padEnd(20)} ${job.updatedAt}`)
  }
}

const { positional, flags } = parseArgs(process.argv.slice(2))
const [command, target] = positional

try {
  if (command === 'step1') await cmdStep1(target, flags)
  else if (command === 'remerge') await cmdRemerge(target)
  else if (command === 'step2') await cmdStep2(target, flags)
  else if (command === 'remerge2') await cmdRemerge2(target)
  else if (command === 'processes') await cmdProcesses(target, flags)
  else if (command === 'step3') await cmdStep3(target, flags)
  else if (command === 'remerge3') await cmdRemerge3(target, flags)
  else if (command === 'nodes') await cmdNodes(target, flags)
  else if (command === 'inspect') await cmdInspect(target, flags)
  else if (command === 'list') await cmdList()
  else {
    console.log(
      'commands:\n' +
        '  step1 <file.pdf>   run exhaustive extraction (tiled VLM passes)\n' +
        '  remerge <jobId>    replay step1 merge/anchor logic over cache, free\n' +
        '  inspect <jobId>    print the extracted inventory\n' +
        '  step2 <jobId>      classify entities into processes\n' +
        '  remerge2 <jobId>   replay step2 validation over cache, free\n' +
        '  processes <jobId>  print the classified processes\n' +
        '  step3 <jobId>      cluster processes into HAZOP nodes (DSU)\n' +
        '                     add --max-nodes N to force-consolidate down to a target\n' +
        '  remerge3 <jobId>   replay step3 clustering over cache, free\n' +
        '                     add --max-nodes N to retarget consolidation, also free\n' +
        '  nodes <jobId>      print the clustered HAZOP nodes\n' +
        '                     add --consolidated to print the --max-nodes result\n' +
        '  list               show all jobs',
    )
    process.exitCode = command ? 1 : 0
  }
} catch (err) {
  console.error(`error: ${err.message}`)
  process.exitCode = 1
}
