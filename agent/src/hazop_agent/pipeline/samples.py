"""Reference library of accepted HAZOP worksheets, for step 4 to write like.

Past HAZOP reports (PDF, Excel, Word, CSV, text) go in docs/hazop_samples/.
`build_library` reads each once (cached by its sha256) and extracts its
worksheet rows as written: deviation -> causes -> consequences -> safeguards,
recommendation, with the kind of node they were written for. It then writes
a style guide from all of them: how causes, consequences and safeguards are
phrased in reports the industry accepts.

`examples_for` picks the rows that match a node's required deviations (same
parameter and guide word, preferring the same kind of node), and
`prompt_section` gives step 4 the style guide and those rows to follow.

The reports are sent to the model once each, when the library is built, and
the picked rows with every worksheet drafted; keep that in mind for client
documents.
"""
import hashlib
import json
from pathlib import Path

from .documents import extract_text
from .llm import structured_call, text_part

SAMPLES_DIR = Path(__file__).resolve().parents[1] / 'docs' / 'hazop_samples'
LIBRARY = SAMPLES_DIR / 'library.json'
READABLE = {'.pdf', '.docx', '.xlsx', '.xlsm', '.csv', '.txt'}
CHUNK_CHARS = 30_000  # one extraction call per chunk of a long report
NODE_TYPES = ['storage', 'transfer', 'unloading', 'loading', 'filtration', 'heat-transfer', 'reaction', 'agitation',
              'distribution', 'utility', 'containment', 'other']

_STR = {'type': 'string'}
_STRS = {'type': 'array', 'items': _STR}

ROWS_SCHEMA = {
    'type': 'object',
    'properties': {
        'rows': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'node': {'type': 'string', 'description': 'The node as the report names it, e.g. "Node 3: Crude transfer pumps P-101A/B".'},
                    'nodeType': {'type': 'string', 'enum': NODE_TYPES},
                    'parameter': {'type': 'string', 'description': 'Lower case: flow, level, pressure, temperature, composition, mixing, ...'},
                    'guideword': {'type': 'string', 'description': 'Upper case: NO, MORE, LESS, REVERSE, OTHER THAN, AS WELL AS, PART OF, ...'},
                    'deviation': _STR,
                    'causes': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'cause': {'type': 'string', 'description': 'Exactly as written in the report.'},
                                'consequences': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'consequence': {'type': 'string', 'description': 'Exactly as written.'},
                                            'safeguards': {**_STRS, 'description': 'Each exactly as written.'},
                                            'recommendation': {'type': 'string', 'description': 'Exactly as written; "" if none.'},
                                        },
                                        'required': ['consequence', 'safeguards', 'recommendation'],
                                        'additionalProperties': False,
                                    },
                                },
                            },
                            'required': ['cause', 'consequences'],
                            'additionalProperties': False,
                        },
                    },
                },
                'required': ['node', 'nodeType', 'parameter', 'guideword', 'deviation', 'causes'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['rows'],
    'additionalProperties': False,
}

EXTRACT_PROMPT = """You read part of a HAZOP study report and copy out its worksheet rows.
Keep the report's own wording for every deviation, cause, consequence,
safeguard and recommendation: do not rephrase, correct or summarise them;
they are kept as examples of how accepted reports are written. Group the
rows as the worksheet does: a deviation with its causes, a cause with its
consequences, a consequence with its safeguards and recommendation. Where a
cell continues a row above (a blank parameter or deviation), it belongs to
that row. Classify each node by what it does (nodeType). Skip anything that
is not a worksheet row (cover pages, attendance, P&ID lists). Return no rows
if this part has none."""

STYLE_SCHEMA = {
    'type': 'object',
    'properties': {'styleGuide': {'type': 'string', 'description': 'The style guide, as short rules with an example each.'}},
    'required': ['styleGuide'],
    'additionalProperties': False,
}

STYLE_PROMPT = """These are rows from HAZOP worksheets that were accepted in industry. Write a
style guide a HAZOP scribe can follow to write new rows the same way. Cover,
each as a short rule with a real example from the rows:
- how a cause is phrased: which item is named (tag, line, equipment), the
  failure mode words used, how operator error and upstream / downstream
  causes are written, how specific each cause is, one cause per row;
- how a consequence is phrased: the chain from the deviation to the
  hazardous outcome, what is named (equipment, release, fire, injury,
  environment, production), how far the chain is taken;
- how safeguards are written: tag plus function, alarms versus trips versus
  relief versus procedures, what is not counted as a safeguard;
- how recommendations are written: the action verb, who / what, when one
  is raised;
- the granularity and vocabulary that recur (e.g. "fails closed",
  "inadvertently opened", "potential for").
Describe what the rows actually do; do not add rules of your own."""


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_library():
    return json.loads(LIBRARY.read_text()) if LIBRARY.exists() else {'files': {}, 'styleGuide': ''}


def _chunks(text):
    return [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)] or ['']


async def _extract(path):
    text = extract_text(path) or ''
    rows = []
    for i, chunk in enumerate(_chunks(text)):
        if not chunk.strip():
            continue
        result = await structured_call(system=EXTRACT_PROMPT, schema=ROWS_SCHEMA, schema_name='hazop_sample_rows',
                                       content=[text_part(f'Report {path.name}, part {i + 1}:\n\n{chunk}')])
        rows += result['rows']
    return rows


async def build_library(force=False, on_progress=print):
    """Extracts every report in the samples folder not yet in the library (all
    of them with `force`), drops removed ones, and rewrites the style guide."""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    library = load_library()
    files = {}
    for path in sorted(p for p in SAMPLES_DIR.iterdir() if p.suffix.lower() in READABLE):
        sha = _sha(path)
        cached = library['files'].get(sha)
        if cached and not force:
            files[sha] = cached
            continue
        on_progress(f'reading {path.name} …')
        rows = await _extract(path)
        on_progress(f'  {len(rows)} deviation rows')
        files[sha] = {'fileName': path.name, 'rows': rows}
    changed = files.keys() != library['files'].keys() or force
    library['files'] = files
    rows = [r for f in files.values() for r in f['rows']]
    if rows and (changed or not library.get('styleGuide')):
        on_progress('writing the style guide …')
        sample = json.dumps(rows[:150], ensure_ascii=False)[:60_000]
        library['styleGuide'] = (await structured_call(system=STYLE_PROMPT, schema=STYLE_SCHEMA, schema_name='hazop_style',
                                                       content=[text_part(sample)]))['styleGuide']
    elif not rows:
        library['styleGuide'] = ''
    LIBRARY.write_text(json.dumps(library, indent=1, ensure_ascii=False))
    return library


def _node_types(node):
    """The sample node types a pipeline node is closest to, from its categories."""
    categories = set(node.get('categories') or [])
    mapping = {'storage': 'storage', 'transfer': 'transfer', 'unloading': 'unloading', 'filtration': 'filtration',
               'heat-transfer': 'heat-transfer', 'agitation': 'agitation', 'utility-supply': 'utility', 'drain': 'containment'}
    return {mapping[c] for c in categories if c in mapping}


def examples_for(node, required, per_deviation=2, max_rows=24, library=None):
    """Sample rows for the node's required deviations: same parameter and guide
    word, the same kind of node first. `required` is [(parameter, guideword, detail)]."""
    library = library or load_library()
    rows = [{**r, 'source': f['fileName']} for f in library['files'].values() for r in f['rows']]
    if not rows:
        return []
    types = _node_types(node)
    picked = []
    for parameter, guideword, _ in dict.fromkeys((p.lower(), g.upper(), '') for p, g, _ in required):
        matches = [r for r in rows if r['parameter'].lower() == parameter and r['guideword'].upper() == guideword and r['causes']]
        matches.sort(key=lambda r: (r['nodeType'] not in types, -len(r['causes'])))
        picked += matches[:per_deviation]
        if len(picked) >= max_rows:
            break
    return picked[:max_rows]


def format_examples(rows):
    out = []
    for r in rows:
        lines = [f"[{r['source']} — {r['node']}] {r['parameter']} / {r['guideword']}: {r['deviation']}"]
        for c in r['causes'][:4]:
            lines.append(f'  Cause: {c["cause"]}')
            for q in c['consequences'][:2]:
                lines.append(f'    Consequence: {q["consequence"]}')
                lines += [f'      Safeguard: {s}' for s in q['safeguards'][:4]]
                if q['recommendation']:
                    lines.append(f'      Recommendation: {q["recommendation"]}')
        out.append('\n'.join(lines))
    return '\n\n'.join(out)


def prompt_section(node, required):
    """The style guide and matching accepted rows, for step 4's prompt ('' when
    there is no library yet)."""
    library = load_library()
    rows = examples_for(node, required, library=library)
    if not library.get('styleGuide') and not rows:
        return ''
    parts = ['STYLE REFERENCE — HAZOP worksheets accepted in industry. Write this node\'s causes, consequences, '
             'safeguards and recommendations the way these are written: the same phrasing, specificity and vocabulary. '
             'They are examples of style only: everything you write must be about THIS node and its own tags.']
    if library.get('styleGuide'):
        parts.append('Style guide:\n' + library['styleGuide'])
    if rows:
        parts.append('Accepted rows for the same deviations:\n\n' + format_examples(rows))
    return '\n\n'.join(parts)

