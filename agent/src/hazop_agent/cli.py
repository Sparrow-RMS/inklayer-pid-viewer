"""Command line entry point for the HAZOP node agent.

    uv run hazop-agent run drawing.pdf                 # full pipeline, reusing cached steps
    uv run hazop-agent run <jobId> --max-nodes 9       # add consolidation to an existing job
    uv run hazop-agent run <jobId> --replay            # re-apply deterministic rules, no model calls
    uv run hazop-agent run <jobId> --force-from step2  # redo step 2 onward
    uv run hazop-agent worksheet <jobId> p0-node-1     # step 4: draft a node's HAZOP worksheet (--save adds it)
    uv run hazop-agent samples                         # build the reference library from docs/hazop_samples/
    uv run hazop-agent graph                           # print the graph as Mermaid
    uv run hazop-agent serve                           # HTTP endpoint for the viewer (port 4002)
    uv run hazop-agent list                            # jobs on disk

Artifacts live in data/jobs/<jobId>/ at the repo root (PIPELINE_JOBS_DIR moves it).
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .graph.builder import graph
from .graph.state import STEPS
from .pipeline import jobs
from .pipeline.llm import PROVIDERS, model_name, provider as current_provider

_AGENT_ROOT = Path(__file__).resolve().parents[2]


def _load_env():
    load_dotenv(_AGENT_ROOT / '.env')


def _step_options(args):
    tiling = {k: v for k, v in (('cols', args.cols), ('rows', args.rows), ('overlap', args.overlap)) if v is not None}
    step1 = {'concurrency': args.concurrency, 'tilePx': args.tile_px, 'pagePx': args.page_px, 'includeVectors': not args.no_vectors, 'tiling': tiling}
    if args.pages:
        step1['pageIndexes'] = [int(p) for p in args.pages.split(',')]
    pairs = {'maxPairsPerCall': args.max_pairs_per_call, 'concurrency': min(args.concurrency, 3)}
    return {
        'step1': step1,
        'step2': {'pagePx': args.classify_page_px},
        'sheet': {'pagePx': args.classify_page_px},
        'step3': {'pagePx': args.classify_page_px, 'repair': not args.no_repair},
        'consolidate': pairs,
    }


def describe_progress(event):
    """One human-readable line for a step's progress event, or None to skip it.
    Shared with the HTTP server, which streams the same lines to the viewer."""
    step, phase = event.get('step'), event.get('phase', '')
    if phase == 'page-start' and step == 'step1':
        return f"[step1] page {event['pageIndex']}: {event['tiles']} tiles, {event['textItems']} text items, {event['vectorSegments']} vector segments"
    if phase == 'tile-done':
        return f"  {event['tile']}: {event['entities']} entities"
    if phase == 'page-start' and step == 'step2':
        return f"[step2] page {event['pageIndex']}: classifying {event['classifiableEntities']} entities"
    if phase == 'page-done' and step == 'step2':
        return f"  -> {event['processes']} processes proposed"
    if phase == 'page-start' and step == 'sheet':
        return f"[sheet] page {event['pageIndex']}: reading the C&E table; {event['redSegments']} red strokes, {event['redText']} red text runs"
    if phase == 'page-done' and step == 'sheet':
        return f"  -> {event['interlocks']} C&E rows"
    if phase == 'page-start' and step == 'step3':
        return f"[step3] page {event['pageIndex']}: building nodes from {event['processes']} processes and {event['interlocks']} C&E rows"
    if phase == 'nodes-proposed':
        return f"  -> {event['nodes']} nodes proposed, {event['errors']} validation errors" + (' — repairing' if event['errors'] else '')
    if phase == 'nodes-repaired':
        return f"  -> {event['nodes']} nodes after repair"
    if phase in ('chunk-done', 'consolidate-chunk-done'):
        return f"  chunk {event['chunk']}: {event.get('verdicts', event.get('scores'))} answers"
    if phase == 'consolidate-start':
        return f"[consolidate] page {event['pageIndex']}: {event['nodes']} nodes -> scoring {event['pairs']} pairs"
    if phase == 'consolidate-skip':
        return f"[consolidate] page {event['pageIndex']}: already <= {event['targetMaxNodes']} nodes"
    if phase.endswith('failed'):
        return f"  ! {step} {phase} {event.get('tile', '')} — {event.get('error')}"
    return None


async def _run(args):
    target = Path(args.target).expanduser()
    payload = {'pdf_path': str(target)} if target.suffix.lower() == '.pdf' or target.exists() else {'job_id': args.target}
    payload.update({'replay': args.replay, 'options': _step_options(args)})
    if args.max_nodes:
        payload['max_nodes'] = args.max_nodes
    if args.min_relatedness is not None:
        payload['min_relatedness'] = args.min_relatedness
    if args.force_from:
        payload['force_from'] = args.force_from

    # The drawing's thread (its job id), shared with the viewer's chat.
    thread_id = jobs.job_id_for(target.name, target.read_bytes()) if 'pdf_path' in payload else args.target
    config = {'configurable': {'thread_id': thread_id}, 'metadata': {'thread_id': thread_id}, 'run_name': 'hazop-pipeline'}

    final = {}
    async for mode, chunk in graph.astream(payload, config=config, stream_mode=['custom', 'updates', 'values']):
        if mode == 'custom':
            line = describe_progress(chunk)
            if line:
                print(f'  {line}')
        elif mode == 'updates':
            for node, update in chunk.items():
                status = (update or {}).get('step_status') or {}
                for step, how in status.items():
                    print(f'[{node}] {step}: {how}')
        else:
            final = chunk

    print(f"\njob {final.get('job_id')} — {final.get('file_name')}")
    print(f"model: {current_provider()} / {model_name()}")
    print(f"artifacts: {jobs.results_dir(final['job_id']) if final.get('job_id') else '—'}")
    print(json.dumps(final.get('summaries', {}), indent=2))
    for issue in final.get('issues', []):
        print(f"  ! {issue['step']} page {issue.get('page', '?')}: {issue['problem']}")
    if final.get('failure'):
        print(f"\nFAILED: {final['failure']}")
        return 1
    return 0


async def _worksheet(args):
    if args.save:
        from .worksheets import GenerateIn, generate_node_worksheet
        out = await generate_node_worksheet(args.job, args.node, GenerateIn(replace=args.replace))
        deviations, warnings = out['deviations'], out['warnings']
        print(f"added {out['added']} deviations, skipped {out['skipped']} already in the worksheet")
    else:
        from .pipeline.step4.generate import generate
        out = await generate(args.job, args.node)
        deviations, warnings = out['rows'], out['warnings']
    for i, d in enumerate(deviations, 1):
        print(f"{i}. {d['parameter']} | {d['guideword']} | {d['deviation']}")
        for j, c in enumerate(d['causes'], 1):
            print(f"   {i}.{j} {c['text']}")
            for k, q in enumerate(c['consequences'], 1):
                print(f"      {i}.{j}.{k} {q['text']}")
                for s in q['safeguards']:
                    print(f"         safeguard: {s['text']}")
                for r in q['recommendations']:
                    print(f"         recommendation: {r['text']}")
    for w in warnings:
        print(f"! {w}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='hazop-agent', description='P&ID -> HAZOP nodes, as a LangGraph agent.')
    sub = parser.add_subparsers(dest='command', required=True)
    # Which model API the pipeline calls; overrides PIPELINE_PROVIDER in .env.
    provider = argparse.ArgumentParser(add_help=False)
    provider.add_argument('--provider', choices=PROVIDERS, help='model API for the pipeline (default: PIPELINE_PROVIDER, else openai)')

    run = sub.add_parser('run', parents=[provider], help='run the pipeline graph on a PDF or an existing job id')
    run.add_argument('target', help='path to a PDF, or a job id under data/jobs')
    run.add_argument('--max-nodes', type=int, help='consolidate each page to at most this many nodes')
    run.add_argument('--min-relatedness', type=float, help='below this, a consolidation merge is flagged forced (default 0.3)')
    run.add_argument('--force-from', choices=STEPS, help='recompute this step and every step after it')
    run.add_argument('--replay', action='store_true', help='re-run the deterministic half of each step over cached model responses')
    run.add_argument('--pages', help='step 1: comma-separated page indexes (default all)')
    run.add_argument('--cols', type=int)
    run.add_argument('--rows', type=int)
    run.add_argument('--overlap', type=float)
    run.add_argument('--concurrency', type=int, default=4)
    run.add_argument('--tile-px', type=int, default=1600)
    run.add_argument('--page-px', type=int, default=2200, help='step 1 whole-sheet render size')
    run.add_argument('--classify-page-px', type=int, default=2400, help='step 2 whole-sheet render size')
    run.add_argument('--no-vectors', action='store_true')
    run.add_argument('--max-pairs-per-call', type=int, default=40)
    run.add_argument('--no-repair', action='store_true', help='step 3: skip the repair pass when validation finds errors')

    ws = sub.add_parser('worksheet', parents=[provider], help="step 4: draft one node's HAZOP worksheet")
    ws.add_argument('job', help='job id under data/jobs')
    ws.add_argument('node', help='node id, e.g. p0-node-1')
    ws.add_argument('--save', action='store_true', help="add the draft to the node's worksheet (skips deviations already there)")
    ws.add_argument('--replace', action='store_true', help="with --save: delete the node's existing deviations first")

    samples = sub.add_parser('samples', help='build the reference library of accepted HAZOP worksheets from docs/hazop_samples/')
    samples.add_argument('--force', action='store_true', help='read every report again, not only new or changed ones')

    sub.add_parser('graph', help='print the graph as Mermaid')
    sub.add_parser('list', parents=[provider], help='list jobs on disk')
    cmp = sub.add_parser('compare', help="compare OpenAI's and Claude's node breakdowns of one drawing")
    cmp.add_argument('job', help='job id under data/jobs, run on both providers')
    cmp.add_argument('--consolidated', action='store_true', help='compare the consolidated (max nodes) breakdowns')
    cmp.add_argument('--json', action='store_true', help='print the comparison as JSON')
    serve = sub.add_parser('serve', parents=[provider], help='serve the graph over HTTP for the viewer')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=int(os.environ.get('AGENT_PORT', 4002)))

    args = parser.parse_args(argv)
    _load_env()
    if getattr(args, 'provider', None):
        os.environ['PIPELINE_PROVIDER'] = args.provider

    if args.command == 'compare':
        from .pipeline.compare import compare, format_report
        report = compare(args.job, 'step3-consolidated' if args.consolidated else 'step3')
        print(json.dumps(report, indent=2) if args.json else format_report(report))
        return 1 if report.get('missing') else 0

    if args.command == 'graph':
        print(graph.get_graph().draw_mermaid())
        return 0
    if args.command == 'serve':
        import uvicorn

        from .server import app
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.command == 'samples':
        from .pipeline.samples import LIBRARY, SAMPLES_DIR, build_library
        library = asyncio.run(build_library(args.force))
        rows = sum(len(f['rows']) for f in library['files'].values())
        print(f"{len(library['files'])} report(s), {rows} deviation rows -> {LIBRARY}")
        if not library['files']:
            print(f'Put past HAZOP reports (PDF, Excel, Word) in {SAMPLES_DIR} and run this again.')
        return 0
    if args.command == 'worksheet':
        return asyncio.run(_worksheet(args))
    if args.command == 'list':
        for job in jobs.list_jobs():
            print(f"{job['jobId']:<48} {','.join(job['artifacts']) or '—'}")
        return 0
    return asyncio.run(_run(args))


if __name__ == '__main__':
    raise SystemExit(main())
