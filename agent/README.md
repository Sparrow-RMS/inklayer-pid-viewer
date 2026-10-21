# HAZOP node agent (LangGraph, Python)

P&ID → HAZOP nodes as a LangGraph graph, one node per pipeline step. It
started as a port of `server/src/pipeline/`. Step 3 now applies the HAZOP
node-creation rules in `src/hazop_agent/pipeline/step3/node_rules.md`.

```
START → ingest → extract → classify → sheet_data → cluster → [consolidate] → END
                 (step 1)   (step 2)   (sheet)      (step 3)   (reduced option, only with max_nodes)
```

- **sheet** reads what the rules need beyond the entity inventory:
  - the cause-and-effect / interlock table, row by row and grouped by mode;
  - the new or modified scope, found in code from the drawing's red strokes,
    red text and revision clouds;
  - the C&E checks that can be done exactly (V3): tags missing from the
    drawing, a valve required both OPEN and CLOSED, a permissive and its trip
    checking different valves, new equipment with no interlock.
- **step 3** proposes the whole sheet's nodes in one call, with every Step 6
  field. Code then validates the answer against Step 7: coverage (V1),
  interlock map (V2), protection gaps (V4), boundary sanity (V5), revised
  scope kept separate (R6), and size (Step 5). If a check fails with an
  error, the model gets one repair pass with the findings (`--no-repair`
  turns this off). The report is in each page's `validation`.
- **consolidate** is the rules' "reduced option". It never merges revised
  scope into existing scope, even when that leaves the target unmet.

To change the rules, edit `node_rules.md`. It is the step 3 system prompt.
A job with cached raw responses picks up rule-check changes for free with
`--replay`; prompt changes need `--force-from step3`.

Every edge after a step checks `failure` first. A step whose output is
unusable (no entities, no processes) ends the run before the next step
spends money.

## Run

```bash
uv sync
cp .env.example .env            # then set OPENAI_API_KEY
uv run hazop-agent run drawing.pdf
uv run hazop-agent run <jobId> --max-nodes 9       # add or retarget consolidation
uv run hazop-agent run <jobId> --replay            # re-apply deterministic rules, no model calls
uv run hazop-agent run <jobId> --force-from step2  # redo step 2 and everything after it
uv run hazop-agent graph                           # Mermaid diagram
uv run hazop-agent serve                           # HTTP endpoint for the viewer (port 4002)
uv run hazop-agent list
uv run pytest
```

From Python:

```python
from hazop_agent.graph.builder import graph

state = await graph.ainvoke({"pdf_path": "drawing.pdf", "max_nodes": 9})
async for mode, chunk in graph.astream({"job_id": "..."}, stream_mode=["custom", "updates"]):
    ...  # "custom" carries tile-by-tile progress events
```

## From the viewer

Run `uv run hazop-agent serve` next to `npm run dev`. Vite proxies `/agent`
to port 4002. Open a PDF, set **Max nodes** (leave it empty to skip
consolidation) and click **Run HAZOP pipeline**. The viewer uploads the PDF to
`POST /agent/run`, shows progress in the chat, and paints the nodes when the
run finishes. The endpoint streams newline-delimited JSON; see
`src/hazop_agent/server.py`. It uses the same job directory as the CLI, so
opening the same drawing again reuses its cached steps.

## OpenAI or Claude

`PIPELINE_PROVIDER=anthropic` in `.env` (or `--provider anthropic` on `run`)
sends every pipeline call to Claude instead of OpenAI; see `.env.example` for
its settings. Each provider's results live in their own place in the job
(OpenAI's at the top of the job directory, Claude's under `anthropic/`), so
switching never replays the other's cached answers. To check one against the
other on a drawing, run it on both and compare:

```bash
uv run hazop-agent run drawing.pdf                          # OpenAI
uv run hazop-agent run <jobId> --provider anthropic         # Claude, same drawing
uv run hazop-agent compare <jobId>                          # side by side
```

`compare` lines the two node breakdowns up by tag and reports, per page, the
tags each read (and how many match the PDF's text layer), node counts, Step 7
validation findings, and each node pairing with the tags only one put in it.
It measures agreement, not correctness: there is no answer key.

The viewer uses whichever provider the server was started with
(`GET /agent/health` says which); restart it after changing it. Worksheets are
stored by node id (`p0-node-1`, ...), which both providers use, so draft
worksheets on one provider at a time.

## 3D plant view

**3D view** in the viewer's toolbar sends the open PDF to Claude
(`src/hazop_agent/plant3d.py`, `POST /agent/plant3d`), which writes the plant
as one self-contained Three.js page: equipment, piping coloured by service,
tags, legend, click-to-inspect. It needs `ANTHROPIC_API_KEY` in `.env`
(`PLANT3D_MODEL` overrides the default `claude-opus-5-5`). The page is cached
as `plant3d.html` in the drawing's job directory, so reopening the drawing
shows it again for free; **Regenerate** redraws it. The viewer shows it in an
iframe sandboxed to scripts only.

## Nodes in 3D

**Nodes in 3D** in the toolbar shows the HAZOP nodes on a 3D model built from
the pipeline's own results (`src/hazop_agent/plant_scene.py`,
`/agent/plant-scene/{jobId}`), separate from the 3D view above. Every item is a
step 1 entity and every pipe a traced span, laid out where the drawing puts
them, so a node's `entityIds` and route spans are exactly its objects: the
viewer (`src/lib/plantScene3d.ts`) colours them like the 2D nodes, follows
merges, and flies to a node when it is picked. Claude only sizes the
equipment (shape, real size, elevation; one call per page, cached as
`plant-scene-forms` for that step 1 run); until it has, sizes come from the
type text. It needs the pipeline to have run on the drawing first.

`langgraph.json` is included, so `langgraph dev` opens the graph in LangGraph Studio.

## Tracing

Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env` (optionally
`LANGSMITH_PROJECT`) to send traces to LangSmith. Each graph run becomes one
trace, with a run per step node and the OpenAI calls nested under it (the
client in `pipeline/llm.py` is wrapped with `wrap_openai`). Traces include the
rendered drawing images, so they can be large.

## How each step node decides what to do

Every step keeps the JS pipeline's split: a paid **collect** half that
renders the PDF and calls the model, and a free, pure **assemble** half. The
raw model responses are cached as `<step>-raw.json`. Each node then:

1. **computes** (paid) if it is forced, or if an earlier step changed its
   output during this run, which makes this step's artifact stale;
2. **replays** the assemble half over the raw cache when `replay` is set or
   the artifact is missing. No model calls. Later steps recompute only if
   the replay actually changed the artifact;
3. **reuses** the artifact otherwise.

Consolidation always replays when it can, because its scores don't depend on
the target. Retargeting `max_nodes` is free once a page has been scored,
provided the scores were computed for the nodes step 3 has now.

State carries references and summaries, never the artifacts themselves.
`step1.json` is megabytes, and a checkpointer would copy it after every node.

## Relation to the JS pipeline

Both pipelines use the same job directory (`data/jobs/<jobId>/` at the repo root), the same
content-hash job id, and the same step 1 and step 2 artifacts. The agent
reuses a drawing's step 1 and 2 from either side.

From step 3 on, the agent has moved on. It writes rules-based `step3.json`
(`"step": "step3-nodes"`) with its raw cache in `step3-nodes-raw.json`, and
adds `step1b-sheet.json`. A pairwise `step3.json` or consolidation left by
the JS pipeline is never reused; it is rebuilt. The JS CLI can't read the
new step 3.

`tests/test_parity_js.py` still replays cached raw responses through both
implementations for steps 1–2, the legacy pairwise clustering
(`step3/cluster.py`, no longer in the graph), and the consolidation merge
arithmetic. All must match exactly.

The PDF layer uses PyMuPDF instead of pdfjs. On the real tank-farm sheet,
text runs match pdfjs to about 1e-8 of the page, and all 6,737 stroked
segments match too. Text ids are numbered differently (pdfjs counts blank
separator items). Ids only need to be consistent within one job, so this
doesn't matter.

## Layout

```
src/hazop_agent/
  graph/      state.py, nodes.py (one node per step), builder.py
  pipeline/   pdf.py (PyMuPDF), llm.py, jobs.py, geometry.py, dsu.py, tiling.py
              step1/ extract, step2/ classify
              sheet/ revisions (red markup), extract (C&E table + V3 checks)
              step3/ node_rules.md, build (rules builder), rules (facts + Step 7 checks),
                     consolidate (reduced option); cluster, candidates (legacy pairwise)
  cli.py, server.py
tests/        ports of the JS unit tests, PDF-layer, JS-parity, rule-check, graph and server tests
```

The step 1 and 2 prompts and schemas are verbatim copies of the JS ones.
The consolidation prompt adds the rules' merge criteria. The sheet and
step 3 builder prompts exist only here.
