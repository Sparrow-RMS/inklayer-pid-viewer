# P&ID analysis pipeline

Four stages, each written to disk so a later one can be re-run and re-tuned
without re-paying for the earlier ones.

| Stage | Status | Output |
|---|---|---|
| **1. Extract** — everything on the sheet | **done** | `step1.json` |
| **2. Classify processes** | **done** | `step2.json` |
| **3. Cluster processes into HAZOP nodes** (DSU) | **done** | `step3.json` |
| **4. Annotate in the editor** | partly exists in the frontend | drawn annotations |

```bash
node test/fixtures/make-pid.mjs /tmp/pid.pdf   # synthetic drawing, no API key needed
npm run pipeline step1 /tmp/pid.pdf            # the paid pass
npm run pipeline inspect <jobId>               # read the entity inventory
npm run pipeline remerge <jobId>               # replay step1 merge logic, free

npm run pipeline step2 <jobId>                 # classify entities into processes (needs step1 first)
npm run pipeline processes <jobId>             # read the classified processes
npm run pipeline remerge2 <jobId>              # replay step2 validation logic, free

npm run pipeline step3 <jobId>                 # cluster processes into HAZOP nodes (needs step2 first)
npm run pipeline nodes <jobId>                 # read the clustered nodes
npm run pipeline remerge3 <jobId>              # replay step3 clustering logic, free

npm test                                       # deterministic logic for all three steps, no API key
```

Artifacts live in `server/.jobs/<jobId>/`, where `<jobId>` is derived from the
PDF's **content hash** — re-running the same drawing reuses the directory.

## The coordinate problem, and why this works

Step 4 can only draw a node in the right place if step 2 knows where that
process is. Asking a vision model for coordinates gives boxes that are
roughly right and precisely wrong, and the error compounds at every stage.

So coordinates are captured **once, in step 1, from the PDF itself**, and
every later stage refers back to them by entity id rather than re-deriving
them:

- **Text layer** (`page.getTextContent`) — every tag, line number and note
  with its exact box. An entity that anchored its tag to real text carries a
  `tagBbox` accurate to the PDF's own precision. 76% of entities on the test
  sheet are anchored this way.
- **Vector geometry** (`page.getOperatorList`) — every stroked line segment
  at full precision. This is the real drawn pipework, and it is what will let
  step 3 decide two processes are *connected* rather than merely near.
- **Rendered tiles** — what the model actually looks at, used for meaning
  (what kind of pump, which way the flow goes), not for position.

## Why tiles

A P&ID page read whole loses its small content: once an A1 sheet is one
image, instrument bubbles are a few pixels tall. Each page is cut into
overlapping tiles (3×2 by default, 12% overlap), each rendered at up to
1600px and read separately, plus one whole-sheet pass for the title block,
notes, legend and revisions.

Overlap means nothing is only ever seen in halves, at the cost of duplicate
sightings — which `assemblePage` then has to merge.

## Merging, and the failures it has to survive

`collectPage` (paid, calls the model) is separate from `assemblePage` (free,
pure). Raw responses are cached in `step1-raw.json`, so `remerge` replays the
whole deterministic half in milliseconds. Every rule below was added in
response to a real failure observed on the test sheet.

**Duplicate sightings are unioned** (`DisjointSet`) on three signals:
1. two sightings anchored to the **same text item** are the same thing;
2. same category, same tag, close together — lines exempt from the distance
   check, since one line number names one line however far it runs;
3. same category, different tiles, boxes overlapping by IoU ≥ 0.45 **or**
   containment ≥ 0.6. Containment matters: two tiles described E-201 at IoU
   0.451, one boxing the shell and the other the shell plus nozzles, and IoU
   alone left it as two exchangers.

**Then four passes repair what the model got wrong:**

- *Anchor exclusivity* — one text item, one owner. A confident sighting
  otherwise absorbs its neighbour's tag: TT-204 anchored `TIC`, `204` and its
  own `204`, erasing TIC-204 from the inventory; P-101A claimed the text
  `P-101B`. Contested anchors go to the nearest entity.
- *Composite bubble tags* — an ISA bubble prints `FT` above `202`, stored as
  two unrelated text runs, so neither half looks like a tag to any check.
  These are paired back into `FT-202` first, because bubbles are the most
  numerous thing on the sheet.
- *Release + attach* — a bubble tag an entity absorbed that spells a
  different tag than its own is handed back (exclusivity cannot catch this:
  only one entity ever claimed it, so there was no competing claim), then
  offered to an untagged symbol sitting under it.
- *Synthesis* — tag-shaped text still unowned is emitted as a low-confidence
  stub flagged `synthesizedFromText`, carrying its exact coordinates. Step 1's
  contract is coverage: a reviewer would rather see a stub to correct than
  discover later that the sheet had an instrument nobody listed.

`diagnostics` on each page records what every one of these passes did, which
is the honest measure of what the extraction missed.

## Output shape

```jsonc
{
  "pages": [{
    "width": 1190, "height": 842,          // scale-1 viewport, points
    "drawingMeta": { "titleBlock": [], "notes": [], "legend": [], "revisions": [] },
    "entities": [{
      "id": "p0-eq-1",
      "category": "equipment",             // equipment|instrument|valve|line|fitting|connector|note|label|…
      "tag": "T-101",
      "type": "vertical surge drum",
      "attributes": [{ "key": "designPressure", "value": "10.5 barg" }],
      "bbox":       { "x": 0.075, "y": 0.157, "width": 0.13, "height": 0.20 },
      "symbolBbox": { },                   // where the symbol is drawn (estimated)
      "tagBbox":    { },                   // where its tag is printed (EXACT, from the text layer)
      "routes":     [],                    // lines only: traced polylines
      "anchored": true,                    // true => position came from the text layer
      "connectsTo": ["P-101A"],
      "resolvedConnections": ["p0-eq-3"],
      "confidence": 0.99,
      "flags": { "lowConfidence": false, "tagMismatch": false, "synthesizedFromText": false }
    }],
    "textItems":      [],                  // exact, every text run on the page
    "vectorSegments": [],                  // exact, every stroked segment
    "diagnostics":    { }
  }]
}
```

All coordinates are normalized fractions of the page, top-left origin — the
same space `src/lib/annotationFromNode.ts` already consumes on the frontend,
so step 4 needs no conversion.

## What steps 2–4 should do with this

- **Step 2** should emit processes that reference `entities[].id` and derive
  geometry from those entities, never re-stating coordinates. A process's
  position is then exactly as good as step 1's, and stays consistent with it.
- **Step 3** should build candidate pairs from `resolvedConnections` and
  `vectorSegments` (real pipe connectivity) rather than comparing all pairs,
  then ask the model only about genuine candidates and union the "yes"
  verdicts with `DisjointSet`.
- **Step 4** already has most of what it needs: `nodeToAnnotation` draws a
  path or a rect per node. The gap is that a clustered node has *several*
  geometries, so it needs to emit one annotation per member route sharing a
  colour and node number.


## Step 2 — process classification

Groups step 1's entities into named PROCESSES: a transfer, a storage
function, an agitation duty, a filtration/backwash path, a utility supply, a
relief path, a drain, a control loop. Same rule as step 1: a process carries
**no coordinates of its own** — it references `entityIds` from step 1, and
`assemblePage` derives its `bbox` and member `routes` as the union of those
entities' real geometry. Step 3's clustering inherits that accuracy for free.

Deliberately finer-grained than a final HAZOP node: keeping a tank's
"storage" function separate from its "agitation" duty and its "transfer to
pump suction" is correct here, not fragmentation — step 3 decides whether
related processes belong in the same node, and can only do that well if step
2 didn't pre-judge it. A process's `primaryEquipmentIds` names its anchor
equipment for that later judgment; `inletEntityIds`/`outletEntityIds` and
`flowDirection` capture which way material moves, using the same
arrow-then-equipment-then-naming priority as step 1's line direction.

The whole entity inventory for a page is shown in one call (not tiled) —
process boundaries routinely span the whole sheet, and a tile-scoped view
would fragment them. `note`, `label`, and `equipment_list_row` entities are
excluded from what the model sees, since they annotate other entities rather
than doing anything themselves — **except** a step 1 entity synthesized from
unclaimed tag-shaped text, which is stored as category `"label"` for lack of
anywhere else to put it, but is real and must still be classifiable (a bug
caught this way on the first real run: `FT-202` came back from step 1 as a
`synthesizedFromText` stub and was silently invisible to step 2 until fixed).

`assemblePage` never trusts the model's own coordinates or an unrecognized
id: every `entityIds`/`primaryEquipmentIds`/`inletEntityIds`/`outletEntityIds`
is checked against step 1's real entity set, a hallucinated id is dropped and
recorded on `flags.hallucinatedIds`, a repeated id within one process's own
list is deduplicated (observed in practice — the model returned
`["TV-1257004", "TV-1257004", ...]`), and a process left with zero real
entities is dropped rather than kept as an empty phantom. `diagnostics`
records `coveredEntityIds`/`uncoveredEntityIds` so a genuine gap (a real
piece of equipment nothing claimed) is visible and distinguishable from
expected residue (low-confidence stubs step 1 itself already flagged).

**A step 1 bug found by testing step 2 against a real drawing:** control
valves, actuated valves, and PSVs carry both a valve body and an
instrument-like actuator/bubble on the same symbol. Different tiles were
observed calling the identical tag "valve" in one sighting and "instrument"
in another (`TV-1257004`, `PSV-1257001`, `HV-1257022`, 11 devices in total on
one real sheet) — step 1's same-tag merge rule required matching categories,
so these never combined and each device appeared twice. Fixed by relaxing
that rule for a narrow, explicit set of known-ambiguous category pairs
(`instrument`/`valve`), still gated by the same proximity check as every
other same-tag merge — confirmed by `remerge` (no new model calls) and
covered by three new tests in `test/merge.test.mjs`.


## Step 3 — cluster processes into HAZOP nodes (DSU)

This is the union-find step: decide which of step 2's processes belong in
the same final HAZOP node, and group them. The model is never asked to
produce a grouping directly — it only answers, independently, "do processes
A and B belong in one node?" for a bounded set of candidate pairs. "Yes"
verdicts are unioned with the same `DisjointSet` step 1 uses to merge
duplicate tile sightings (`src/pipeline/dsu.js`), so the transitive closure
(A~B, B~C ⇒ A,B,C in one node) is arithmetic, never re-derived by the model.

**Candidate pairs are generated deterministically** (`candidates.js`, no
model call) from real signals: two processes sharing an entity, sharing
primary equipment, connected via step 1's resolved connectivity graph,
flagged by step 2 as heavily overlapping, or simply close together on the
sheet. A pair with none of these is never asked about — this is what keeps
27 processes (351 possible pairs) down to a manageable, evidenced candidate
set (135 on the real test sheet) instead of an O(n²) blind sweep.

**The prompt explicitly warns about DSU transitivity** — a real correctness
trap specific to this design: a "yes" on (A,B) plus a "yes" on (B,C) forces
A, B, and C into one node even though the model never directly compared A
and C. A shared connector or busy junction can tempt several locally
reasonable "yes" answers that chain into one node quietly swallowing an
unrelated part of the drawing. The model is told to weigh each "yes" against
what else might already be linked in, and to prefer "no" when in doubt.

A verdict is only applied (unioned) above a confidence threshold, same
low-confidence handling as every prior stage; a hallucinated pair id is
recorded and ignored rather than trusted; a candidate pair the model never
answered defaults to staying separate, never to merging. `diagnostics.
appliedVerdicts` is a full audit trail — which pairs merged, on what
reasoning, and which "yes" answers were withheld for low confidence — the
first thing to check if a node looks wrong.

A node carries no coordinates of its own, the same rule all the way down:
`bbox` is the union of its member processes' real bboxes, and `routes` are
deduplicated by entity id so a boundary line shared by two merged processes
isn't drawn twice by step 4.

**Validated in both directions.** On the synthetic fixture, the model
correctly merged a control loop with the equipment it measures (LIC-101 with
T-101's storage process) and merged four processes into one node after
affirming all 6 pairwise combinations independently — grounded in the real
absence of any valve between them, not an accidental transitive chain — while
correctly keeping the `P-101A`/`P-101B` spare pump pair as separate nodes.

On the real, densely-valved industrial sheet, the result was 27 processes →
27 nodes — zero merges. Investigated rather than trusted blindly: five
targeted probes (backwash vs. normal transfer, a PSV vs. its protected
train, a discharge manifold vs. its feeding train, storage vs. agitation on
the same vessel) each came back "separate" with **specific, grounded
reasoning** citing a real isolation valve tag, a genuinely different
operating mode, or the standard convention of a PSV as its own node — not
generic boilerplate. This is a properly-engineered P&ID with real boundaries
almost everywhere; step 3 correctly recognized that rather than force-merging
adjacent processes for the sake of producing fewer nodes.

### Known simplification

A node's `label`/`description` are composed deterministically (join member
process names/descriptions) rather than written fresh — good enough to
review and correct, not styled prose. An LLM polish pass per final node
(one more batched call per page, after clustering is trusted) is the natural
next refinement if the node write-ups need to read better for a reviewer.


## Consolidating to a target node count

The base clustering (`cluster.js`, above) has no mechanism to guarantee a
target count — it only answers "is there a real HAZOP boundary here?" per
pair, deliberately biased toward "no." When a sheet needs a hard cap (e.g.
"no more than 10 nodes for a practical review session"), `consolidate.js`
runs on top of it, only when needed:

```bash
npm run pipeline step3 <jobId> --max-nodes 9      # base clustering, then consolidate to <= 9
npm run pipeline nodes <jobId> --consolidated     # read the consolidated result
npm run pipeline remerge3 <jobId> --max-nodes 7   # retarget, free — no new model calls
```

This is genuinely agglomerative clustering, not a threshold tweak on the
base pass: every pair among the current nodes is scored for **continuous
relatedness** (0-1, "how strongly do these belong under one broader review
node"), then pairs are merged strongest-first — via the same `DisjointSet` —
until the target is met, stopping the instant it is. The relatedness scoring
asks a genuinely different question than the base pass ("group these into a
workable number of areas" vs. "is there a real boundary"), so it isn't
reusing the base pass's deliberately-conservative judgment for a task it
wasn't calibrated for.

**The guarantee is explicit, not silent.** If reaching the target ever
requires merging two nodes below a sane relatedness floor (default 0.3,
`--min-relatedness` to change), that merge is still made — the target is
honored — but it is flagged `forced: true` in `diagnostics.appliedMerges`
rather than presented as if it were a natural grouping. On the real test
sheet this never happened: consolidating 27 nodes down to 9 needed only 18
merges, every one scoring 0.91+ with specific reasoning citing real
equipment (e.g. "PSV-1257001 directly protects the discharge of P-V305A in
the transfer train described by the other node") — the target was reached
through genuine relatedness, not by force.

**Retargeting is free.** The relatedness matrix doesn't depend on the target
— only where the greedy merge stops does — so once scored, trying 7 vs. 9 vs.
12 as a target is a `remerge3 --max-nodes N` away, no new model calls.

The consolidated result is written to its own artifact
(`step3-consolidated.json`), never overwriting the base `step3.json` — the
fine-grained, individually-justified clustering stays inspectable, and the
consolidated view is an explicit, separate transformation on top of it. Each
consolidated node keeps `subNodeIds`/`processIds` back to everything folded
into it, so no detail from the original breakdown is actually lost — it is
grouped under one broader review scope, not discarded.

Validated on the real test sheet: 27 nodes → 9, by equipment train — one
node absorbed the entire ST-V305 train (storage, agitation, both filter/
pump/relief/backwash sub-trains, distribution routing — 9 processes into
one), another did the same for ST-V303 (10 processes), while utility
supplies, tanker unloading, and the sump drain correctly stayed separate
since they share no equipment with either tank train. 14 new tests cover the
greedy-merge mechanics directly: stopping exactly at target (not below it),
honoring a hard target even with no good relations available (forced merges
flagged), and rejecting hallucinated pair ids.
