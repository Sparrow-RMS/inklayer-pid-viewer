# InkLayer P&ID Viewer

A P&ID review app for HAZOP studies. Open a P&ID PDF in the viewer, run the
agent to divide it into HAZOP nodes (painted on the drawing as whole pipe
runs), review and edit the nodes (by hand or through the chat assistant), and
draft each node's HAZOP worksheet: deviations, causes, consequences,
safeguards and recommendations.

The viewer is built on [inklayer-react](https://github.com/Laomai-codefee/inklayer-react).
The full manual (architecture, API, pipeline, node rules, worksheets, chat,
storage, debugging) is the project's documentation manual.

## Layout

```
inklayer-pid-viewer/
├── src/       React + Vite viewer: PDF, painted nodes, node cards, worksheet, study package, chat
├── agent/     Python agent server (FastAPI + LangGraph): the pipeline, worksheets, study package, chat
├── data/      Created on first run: every drawing's results, worksheets and chats (not in git)
└── server/    The original JS pipeline (server/src/pipeline), kept for reference; nothing runs it
```

## Running it

In two terminals:

```bash
cd agent
cp .env.example .env    # then set OPENAI_API_KEY
uv sync
uv run hazop-agent serve   # http://127.0.0.1:4002
```

```bash
npm install             # from the repo root
npm run dev             # http://localhost:5173
```

Vite forwards `/agent/*` to the agent server. Restart the agent server after
changing any Python code: it keeps the code it started with.

## Data

Everything is stored under `data/jobs/` (set `PIPELINE_JOBS_DIR` in
`agent/.env` to move it): one folder per drawing with its pipeline results,
plus `hazop.sqlite3` (worksheets, study package) and `chat.sqlite3` (chat
threads). Back up `data/` — the worksheets in it are the team's work.

See [`agent/README.md`](agent/README.md) for the pipeline's command line.
