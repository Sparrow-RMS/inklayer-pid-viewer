/**
 * Thin wrapper over the Responses API for the pipeline: structured-output
 * calls with bounded concurrency and retry on the transient failures a long
 * tiled run will inevitably hit (rate limits, 5xx, socket resets).
 */
import OpenAI from 'openai'

let client
const getClient = () => (client ??= new OpenAI())

export const MODEL = () => process.env.PIPELINE_MODEL || process.env.OPENAI_MODEL || 'gpt-5.6'

/** Runs `tasks` with at most `limit` in flight, preserving result order. */
export async function mapLimit(items, limit, worker) {
  const results = new Array(items.length)
  let cursor = 0
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const index = cursor++
      results[index] = await worker(items[index], index)
    }
  })
  await Promise.all(runners)
  return results
}

const RETRYABLE_STATUS = new Set([408, 409, 429, 500, 502, 503, 504])

function isRetryable(err) {
  if (RETRYABLE_STATUS.has(err?.status)) return true
  const code = err?.code ?? err?.cause?.code
  return ['ECONNRESET', 'ETIMEDOUT', 'ECONNREFUSED', 'EPIPE', 'UND_ERR_SOCKET'].includes(code)
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * One structured-output call. Returns the parsed object.
 *
 * `schema` must satisfy the strict JSON-schema subset: every property listed
 * in `required`, and `additionalProperties: false` on every object. That
 * subset has no free-form maps, which is why open-ended fields in the step
 * schemas are modelled as {key, value} arrays rather than objects.
 */
export async function structuredCall({ system, content, schema, schemaName, maxRetries = 4, model = MODEL() }) {
  let lastError
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const response = await getClient().responses.create({
        model,
        input: [
          { role: 'system', content: system },
          { role: 'user', content },
        ],
        text: { format: { type: 'json_schema', name: schemaName, strict: true, schema } },
      })
      return JSON.parse(response.output_text)
    } catch (err) {
      lastError = err
      if (attempt === maxRetries || !isRetryable(err)) break
      // Exponential backoff with jitter, honouring Retry-After when given.
      const retryAfter = Number(err?.headers?.['retry-after']) * 1000
      const backoff = Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : 2 ** attempt * 1000
      await sleep(backoff + Math.random() * 500)
    }
  }
  throw lastError
}

export const textPart = (text) => ({ type: 'input_text', text })
export const imagePart = (dataUrl, detail = 'high') => ({ type: 'input_image', image_url: dataUrl, detail })
