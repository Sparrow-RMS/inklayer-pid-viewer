"""Step 4 — draft one node's HAZOP worksheet, on demand.

Runs after step 3 and never changes it: the node, its boundaries and its
required deviations are taken as fixed. One model call per node writes the
deviations, each with its causes (one per item, naming the valve, line or
equipment tag where it happens), each cause with its consequences, each
consequence with its safeguards and, where protection is missing, a
recommendation. Severity and likelihood are left to the team.

Inputs, all from the job's artifacts: the node's step 3 fields, its own
inventory items, the pipe links traced through them, the C&E table, the
study-package documents and an image cropped to the node; plus, when the
reference library has been built (pipeline/samples.py), its style guide and
the accepted rows for the same deviations, so the draft is written the way
accepted reports are. Code then checks
the draft (`check`) and turns it into worksheet rows (`to_drafts`).
"""
import json

from .. import jobs
from ..documents import format_documents, study_documents
from ..llm import image_part, structured_call, text_part
from ..network import format_links
from ..pdf import load_pdf, render_region, to_data_url
from ..samples import prompt_section as style_reference
from ..step1.extract import normalize_tag
from ..step3.build_prompts import format_interlocks, format_inventory
from .prompts import WORKSHEET_SCHEMA, WORKSHEET_SYSTEM_PROMPT

# Node fields from step 3 the model is given, as they are.
NODE_FIELDS = ('name', 'designIntent', 'boundaryFrom', 'boundaryTo', 'equipmentTags', 'designConditions', 'controlLoops',
               'interlockRefs', 'reliefDevices', 'parameters', 'vacuumProtection', 'operatingModes', 'utilitiesAsSafeguards',
               'typicalOfName', 'differencesFromTypical', 'revisedScope', 'unverified')
CROP_MARGIN = 0.03  # page fractions around the node's extent
CROP_PX = 2000


def find_node(job_id, node_id):
    """(node, page index) from the node breakdown the viewer shows, with the
    reviewer's edits (merges, renames) applied: the consolidated one when it
    holds this id, else step 3's."""
    from ..edits import current_document
    for name in ('step3-consolidated', 'step3'):
        if jobs.has_artifact(job_id, name):
            for page in current_document(job_id, name)['pages']:
                for node in page['nodes']:
                    if node['id'] == node_id:
                        return node, page['pageIndex']
    raise KeyError(f'no node {node_id} in job {job_id}; run the pipeline first')


def _page(doc, page_index):
    return next(p for p in doc['pages'] if p['pageIndex'] == page_index)


def required_deviations(node):
    """(parameter, guideword, detail) step 3 listed for the node (Step 6a)."""
    return [(d['parameter'], d['guideword'].upper(), d.get('detail') or d['parameter']) for d in (node.get('hazop') or {}).get('keyDeviations') or []]


def node_inputs(job_id, node, page_index, pdf=None):
    step1 = _page(jobs.read_artifact(job_id, 'step1'), page_index)
    sheet = _page(jobs.read_artifact(job_id, 'step1b-sheet'), page_index)
    entities = step1['entities']
    mine = set(node['entityIds'])
    revised = {r['entityId'] for r in sheet['revisedScope']['entities']}
    hazop = node.get('hazop') or {}
    parts = [
        text_part(f"The node:\n{node['label']}\n" + json.dumps({k: hazop[k] for k in NODE_FIELDS if k in hazop}, indent=1)),
        text_part('Required deviations (parameter / guide word: what step 3 noted):\n'
                  + ('\n'.join(f'- {p} / {g}: {d}' for p, g, d in required_deviations(node)) or '(none listed; use Step 6a)')),
        *([text_part(style)] if (style := style_reference(node, required_deviations(node))) else []),
        text_part('Entity inventory of this node (id [category] "tag" @x,y — type (attributes) connectsTo=[...]):\n'
                  + format_inventory([e for e in entities if e['id'] in mine], revised)),
        text_part('C&E / interlock table (rows assigned to this node: '
                  + (', '.join(hazop.get('interlockRefs') or []) or 'none') + '):\n' + format_interlocks(sheet['interlockTable'])),
    ]
    if jobs.has_artifact(job_id, 'step1-network'):
        network = _page(jobs.read_artifact(job_id, 'step1-network'), page_index)
        links = [link for link in network['links'] if {link['from'], link['to'], *link['via']} & mine]
        parts.append(text_part('Pipe links traced from the drawing through this node (-> is the flow direction where drawn):\n'
                               + format_links({'links': links}, entities)))
    documents = format_documents(study_documents(job_id))
    if documents:
        parts.append(text_part(documents))
    pdf = pdf or load_pdf(jobs.source_path(job_id))
    b = node['bbox']
    region = {'x': max(0.0, b['x'] - CROP_MARGIN), 'y': max(0.0, b['y'] - CROP_MARGIN)}
    region['width'] = min(1.0, b['x'] + b['width'] + CROP_MARGIN) - region['x']
    region['height'] = min(1.0, b['y'] + b['height'] + CROP_MARGIN) - region['y']
    parts.append(image_part(to_data_url(render_region(pdf[page_index], region, target_px=CROP_PX)['png'])))
    return parts


def check(draft, node, job_id, page_index):
    """Warnings on a draft: causes naming no tag, tags not on the drawing,
    required deviations left out."""
    step1 = _page(jobs.read_artifact(job_id, 'step1'), page_index)
    sheet = _page(jobs.read_artifact(job_id, 'step1b-sheet'), page_index)
    known = {normalize_tag(e['tag']) for e in step1['entities'] if e.get('tag')}
    known |= {normalize_tag(r['ref']) for r in sheet['interlockTable']['interlocks']}
    untagged, unknown = [], []
    for d in draft['deviations']:
        for c in d['causes']:
            if c['cause'].strip().lower().startswith('not credible'):
                continue
            if not c['tags']:
                untagged.append(f"{d['guideword']} {d['deviation']}: {c['cause'][:70]}")
            unknown += [t for t in c['tags'] if normalize_tag(t) not in known]
    out = []
    if untagged:
        out.append(f"{len(untagged)} cause(s) name no valve, line or equipment tag: " + '; '.join(untagged[:6]) + (' …' if len(untagged) > 6 else ''))
    if unknown:
        out.append('Tags not found on the drawing: ' + ', '.join(dict.fromkeys(unknown)))
    have = {(d['parameter'], d['guideword']) for d in draft['deviations']}
    missing = [f'{g} {p}' for p, g, _ in required_deviations(node) if (p, g) not in have]
    if missing:
        out.append('Required deviations not drafted: ' + ', '.join(dict.fromkeys(missing)))
    return out


def _cause_text(cause, tags):
    """The cause, with any of its tags the sentence does not already name."""
    text = cause.strip()
    extra = [t for t in tags if t.strip() and t.strip().lower() not in text.lower()]
    return f"{text} ({', '.join(extra)})" if extra else text


def to_drafts(draft, node):
    """Worksheet rows (the bulk-create shape) from a draft."""
    return [{
        'nodeId': node['id'],
        'nodeLabel': node['label'],
        'parameter': d['parameter'].title(),
        'guideword': d['guideword'].upper(),
        'deviation': d['deviation'].strip(),
        'causes': [{
            'text': _cause_text(c['cause'], c['tags']),
            'consequences': [{
                'text': q['consequence'].strip(),
                'safeguards': [{'text': s.strip()} for s in q['safeguards'] if s.strip()],
                'recommendations': [{'text': q['recommendation'].strip()}] if q['recommendation'].strip() else [],
            } for q in c['consequences']],
        } for c in d['causes'] if c['cause'].strip()],
    } for d in draft['deviations']]


async def generate(job_id, node_id, model=None):
    """Drafts one node's worksheet. Returns {node, draft, warnings, rows} and
    keeps the draft as the job artifact `step4-worksheet-<node id>`."""
    node, page_index = find_node(job_id, node_id)
    draft = await structured_call(system=WORKSHEET_SYSTEM_PROMPT, schema=WORKSHEET_SCHEMA, schema_name='hazop_worksheet',
                                  content=node_inputs(job_id, node, page_index), model=model)
    warnings = check(draft, node, job_id, page_index)
    jobs.write_artifact(job_id, f'step4-worksheet-{node_id}', {
        'step': 'step4-worksheet', 'nodeId': node_id, 'nodeLabel': node['label'], 'generatedAt': jobs.now_iso(),
        'draft': draft, 'warnings': warnings})
    return {'node': node, 'draft': draft, 'warnings': warnings, 'rows': to_drafts(draft, node)}
