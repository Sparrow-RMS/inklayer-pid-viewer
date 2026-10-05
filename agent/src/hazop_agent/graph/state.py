"""Graph state for the HAZOP node agent.

Each step node publishes its output document to state (`step1`, `step2`,
`step3`, `consolidated`) and the next node takes its input from there, so a
single run flows straight through the graph. Every output is also written to
the job directory, exactly as the JS CLI does, so a later run can reuse,
replay or resume from it.

step1 holds every text item and vector segment on the sheet — megabytes on a
real drawing. The graph is compiled without a checkpointer; if you add one,
it will snapshot these documents after every node.
"""
import operator
from typing import Annotated, Literal, TypedDict

StepName = Literal['step1', 'step2', 'sheet', 'step3', 'consolidate']

# Pipeline order. `force_from` and staleness both key off this.
# `sheet` (C&E table + revised scope) needs only step 1, but runs after step 2
# so adding it to an existing job doesn't make step 2 look stale.
STEPS: tuple[StepName, ...] = ('step1', 'step2', 'sheet', 'step3', 'consolidate')


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class HazopInput(TypedDict, total=False):
    # Path to a PDF. Either this or `job_id` starts a run.
    pdf_path: str
    # Resume an existing job from its stashed source.pdf and artifacts.
    job_id: str
    # Consolidate each page to at most this many nodes; unset = skip consolidation.
    max_nodes: int
    min_relatedness: float
    # Recompute this step and everything after it, even if artifacts exist.
    force_from: StepName
    # Re-run the free, deterministic half of every step over its cached raw
    # model responses (the JS `remerge*` commands). No model calls; later
    # steps only recompute if a replay actually changed an artifact.
    replay: bool
    # Per-step tuning passed through to the step modules, keyed by step name,
    # e.g. {"step1": {"tiling": {"cols": 3}}, "step3": {"maxPairsPerCall": 40}}.
    options: dict


class HazopState(HazopInput, total=False):
    file_name: str
    # Set once any step actually changed its output. Every step after it must
    # recompute: its cached artifact was built from inputs that no longer exist.
    upstream_changed: bool
    # step -> how it was satisfied: 'computed' | 'replayed' | 'cached'
    step_status: Annotated[dict, merge_dicts]
    # step -> compact totals, for logs and callers.
    summaries: Annotated[dict, merge_dicts]
    # Non-fatal problems worth a reviewer's attention (failed tiles, model errors).
    issues: Annotated[list, operator.add]
    # Set when a step's output is unusable; the graph routes straight to END.
    failure: str | None
    # Each step's output, handed to the next node as its input.
    step1: dict
    step2: dict
    sheet: dict
    step3: dict
    consolidated: dict
