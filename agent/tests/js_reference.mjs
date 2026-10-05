// Replays a job's cached raw responses through the JS pipeline's pure
// "assemble" halves and prints the result as JSON — the reference the Python
// port is checked against in test_parity_js.py. No model calls.
//   node js_reference.mjs <serverSrcPipelineDir> <jobDir> <step1|step2|step3|consolidate> [targetMaxNodes]
import { readFileSync } from 'node:fs'
const [base, jobDir, step, target] = process.argv.slice(2)
const read = (n) => JSON.parse(readFileSync(`${jobDir}/${n}.json`, 'utf8'))
let out
if (step === 'step1') {
  const { assembleDocument } = await import(`${base}/step1/extract.js`)
  const raw = read('step1-raw')
  out = assembleDocument(raw.pages, raw.fileName, raw.pageCount)
} else if (step === 'step2') {
  const { assembleDocument } = await import(`${base}/step2/classify.js`)
  const raw = read('step2-raw')
  out = assembleDocument(raw.pages, raw.fileName)
} else if (step === 'step3') {
  const { assembleDocument } = await import(`${base}/step3/cluster.js`)
  const raw = read('step3-raw')
  out = assembleDocument(raw.pages, raw.fileName)
} else if (step === 'consolidate') {
  const { assembleDocument } = await import(`${base}/step3/consolidate.js`)
  const raw = read('step3-consolidate-raw')
  out = assembleDocument(raw.pages, raw.fileName, { targetMaxNodes: Number(target), minRelatedness: 0.3 })
}
process.stdout.write(JSON.stringify(out))
