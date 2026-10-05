import 'dotenv/config'
import express from 'express'
import cors from 'cors'
import { analyzePidPages } from './analyze.js'

const app = express()

app.use(cors())
app.use(express.json({ limit: '75mb' }))

app.get('/api/health', (_req, res) => {
  res.json({ ok: true, hasApiKey: Boolean(process.env.OPENAI_API_KEY) })
})

app.post('/api/analyze', async (req, res) => {
  const { fileName, pages } = req.body ?? {}

  if (!Array.isArray(pages) || pages.length === 0) {
    return res.status(400).json({ error: 'pages must be a non-empty array' })
  }

  try {
    const nodes = await analyzePidPages({ fileName: fileName || 'document.pdf', pages })
    res.json({ nodes })
  } catch (err) {
    console.error('[analyze] failed', err)
    res.status(502).json({ error: err.message || 'Analysis failed' })
  }
})

const port = process.env.PORT || 4001
app.listen(port, () => {
  console.log(`[server] listening on http://127.0.0.1:${port}`)
})
