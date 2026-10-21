"""OpenAI vs Claude on the same drawing: how their HAZOP node breakdowns differ.

    uv run hazop-agent compare <jobId>

Each provider's results live in their own place in the job (jobs.py). Entity
ids are minted per run, so the two are lined up by tag: a node is the set of
tags its entities carry, and nodes are paired by how much those sets overlap.

There is no answer key, so this measures agreement and the pipeline's own
checks, not correctness:
  - step 1: tags read, and how many are anchored to the PDF's text layer (a
    tag that matches printed text was read right; one that doesn't may be a
    misread);
  - step 3: node count and the Step 7 validation findings per page;
  - nodes: each pairing's overlap, and the tags only one provider put in a node.
"""
import json

from . import jobs
from .llm import PROVIDERS
from .step1.extract import normalize_tag

# Categories that are drawing furniture, not plant: left out of tag sets.
_NOT_PLANT = {'note', 'label', 'interlock_ref', 'equipment_list_row'}


def _read(job_id, provider_name, name):
    path = jobs.results_dir(job_id, provider_name) / f'{name}.json'
    return json.loads(path.read_text()) if path.exists() else None


def _tags_by_id(step1_page):
    return {e['id']: normalize_tag(e['tag']) for e in step1_page['entities']
            if e.get('tag') and e['category'] not in _NOT_PLANT}


def _step1_page_summary(page):
    plant = [e for e in page['entities'] if e['category'] not in _NOT_PLANT]
    tagged = [e for e in plant if e.get('tag')]
    return {
        'entities': len(plant),
        'tags': len({normalize_tag(e['tag']) for e in tagged}),
        'tagged': len(tagged),
        'anchored': sum(1 for e in tagged if e.get('anchored')),
        'lowConfidence': sum(1 for e in plant if (e.get('flags') or {}).get('lowConfidence')),
    }


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 1.0


def _pair_nodes(left, right):
    """Greedy best-overlap pairing; unpaired nodes come back with None opposite."""
    candidates = sorted(((_jaccard(l['tags'], r['tags']), i, j) for i, l in enumerate(left) for j, r in enumerate(right)),
                        reverse=True)
    used_l, used_r, pairs = set(), set(), []
    for score, i, j in candidates:
        if score > 0 and i not in used_l and j not in used_r:
            used_l.add(i)
            used_r.add(j)
            pairs.append((left[i], right[j], score))
    pairs += [(n, None, 0.0) for i, n in enumerate(left) if i not in used_l]
    pairs += [(None, n, 0.0) for j, n in enumerate(right) if j not in used_r]
    return pairs


def compare(job_id, base='step3'):
    """The comparison as a dict; `base` is 'step3' or 'step3-consolidated'."""
    results = {}
    for name in PROVIDERS:
        step1, nodes = _read(job_id, name, 'step1'), _read(job_id, name, base)
        if step1 and nodes:
            results[name] = (step1, nodes)
    missing = [name for name in PROVIDERS if name not in results]
    if missing:
        return {'jobId': job_id, 'base': base, 'missing': missing}

    (s1_a, n_a), (s1_b, n_b) = results['openai'], results['anthropic']
    pages = []
    for page_a in n_a['pages']:
        index = page_a['pageIndex']
        page_b = next((p for p in n_b['pages'] if p['pageIndex'] == index), None)
        if page_b is None:
            continue
        step1_a = next(p for p in s1_a['pages'] if p['pageIndex'] == index)
        step1_b = next(p for p in s1_b['pages'] if p['pageIndex'] == index)
        tags_a, tags_b = _tags_by_id(step1_a), _tags_by_id(step1_b)
        # Matched normalized, shown as printed on the drawing.
        printed = {normalize_tag(e['tag']): e['tag'] for e in step1_b['entities'] + step1_a['entities'] if e.get('tag')}

        def as_sets(page, tags):
            return [{'label': n['label'], 'tags': {tags[i] for i in n.get('entityIds', []) if i in tags}} for n in page['nodes']]

        pairs = _pair_nodes(as_sets(page_a, tags_a), as_sets(page_b, tags_b))
        # Every tag both providers read: the common ground the nodes can be judged on.
        shared_tags = set(tags_a.values()) & set(tags_b.values())
        pages.append({
            'pageIndex': index,
            'step1': {'openai': _step1_page_summary(step1_a), 'anthropic': _step1_page_summary(step1_b),
                      'tagsInBoth': len(shared_tags)},
            'nodes': {'openai': len(page_a['nodes']), 'anthropic': len(page_b['nodes'])},
            'validation': {'openai': (page_a.get('validation') or {}).get('counts'),
                           'anthropic': (page_b.get('validation') or {}).get('counts')},
            'pairs': [{
                'openai': a and a['label'],
                'anthropic': b and b['label'],
                'overlap': round(score, 2),
                'onlyOpenai': sorted(printed[t] for t in (a['tags'] - b['tags']) & shared_tags) if a and b else [],
                'onlyAnthropic': sorted(printed[t] for t in (b['tags'] - a['tags']) & shared_tags) if a and b else [],
            } for a, b, score in pairs],
        })
    # Overall agreement: overlap averaged over node pairs, unpaired nodes counting 0.
    scores = [p['overlap'] for page in pages for p in page['pairs']]
    return {
        'jobId': job_id,
        'base': base,
        'models': {name: jobs.read_manifest(job_id, name).get('model') for name in PROVIDERS},
        'agreement': round(sum(scores) / len(scores), 2) if scores else None,
        'pages': pages,
    }


def format_report(report):
    if report.get('missing'):
        return (f"{report['jobId']}: no {report['base']} result yet for {', '.join(report['missing'])}. "
                f"Run it with: uv run hazop-agent run {report['jobId']} --provider <provider>")
    lines = [f"{report['jobId']} — {report['base']}, OpenAI vs Claude",
             f"Models: OpenAI {report['models']['openai'] or '?'}, Claude {report['models']['anthropic'] or '?'}",
             f"Node agreement (mean tag overlap of paired nodes, unpaired = 0): {report['agreement']}", '']
    for page in report['pages']:
        s1 = page['step1']
        lines.append(f"Page {page['pageIndex']}")
        for name, label in (('openai', 'OpenAI'), ('anthropic', 'Claude')):
            s, v = s1[name], page['validation'][name] or {}
            lines.append(f"  {label:7} step 1: {s['entities']} items, {s['tags']} unique tags, {s['anchored']}/{s['tagged']} tagged items anchored to PDF text, "
                         f"{s['lowConfidence']} low-confidence | step 3: {page['nodes'][name]} nodes, "
                         f"{v.get('error', '?')} errors, {v.get('warning', '?')} warnings")
        lines.append(f"  tags read by both: {s1['tagsInBoth']}")
        for p in page['pairs']:
            lines.append(f"  {p['overlap']:.2f}  {p['openai'] or '—'}  <->  {p['anthropic'] or '—'}")
            if p['onlyOpenai']:
                lines.append(f"        only OpenAI: {', '.join(p['onlyOpenai'])}")
            if p['onlyAnthropic']:
                lines.append(f"        only Claude: {', '.join(p['onlyAnthropic'])}")
        lines.append('')
    return '\n'.join(lines)
