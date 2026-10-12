"""Reviewer edits to a job's node breakdown: merge and rename nodes.

Edits are kept beside the pipeline's own results (the `node-edits` artifact),
never written into them, and applied on top whenever the nodes are shown, so
a replay or a re-run keeps them. Each edit records a fingerprint of the
nodes it was made on (their entity sets); if a re-run of step 3 changes those
nodes, the edit is skipped and reported as stale rather than merging the
wrong things.

Edits are kept per breakdown ("base"): the rules-based `step3` and the
reduced `step3-consolidated` have different node ids. The base the viewer
last showed is recorded in the job manifest by the server.

A merge keeps the first node's id and number, so its worksheet stays where it
is; the worksheet rows of the other nodes are moved onto it (and moved back on
undo). Numbers are never reassigned: after merging N2 and N3 the drawing has
N1, N2, N4, so a number means the same node all through a conversation.
"""
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing

from . import jobs
from .step3.consolidate import _build_consolidated_node

ARTIFACT = 'node-edits'
BASES = ('step3', 'step3-consolidated')


def fingerprint(node):
    return hashlib.sha256(json.dumps(sorted(node['entityIds'])).encode()).hexdigest()[:12]


def view_base(job_id):
    """The breakdown the viewer last showed for this job."""
    base = jobs.read_manifest(job_id).get('viewBase')
    return base if base in BASES and jobs.has_artifact(job_id, base) else 'step3'


def set_view_base(job_id, base):
    jobs.ensure_job(job_id, viewBase=base)


def load_edits(job_id, base):
    data = jobs.read_artifact(job_id, ARTIFACT) if jobs.has_artifact(job_id, ARTIFACT) else {}
    return data.get(base, [])


def _save_edits(job_id, base, edits):
    data = jobs.read_artifact(job_id, ARTIFACT) if jobs.has_artifact(job_id, ARTIFACT) else {}
    data[base] = edits
    jobs.write_artifact(job_id, ARTIFACT, data)


def _split_label(label):
    """("N3", "350-TK-02 HSD …") for "N3 350-TK-02 HSD …"; ("", label) without a number."""
    head, _, rest = label.partition(' ')
    return (head, rest) if head[:1] == 'N' and head[1:].isdigit() else ('', label)


def _label_name(node):
    """A node's label without its "N3 " number."""
    return _split_label(node['label'])[1]


def _relabel(node, name):
    """The node's label with a new name, keeping its number: a node keeps its
    number through every edit, so "node 3" means the same node all along."""
    number = _split_label(node['label'])[0]
    return f'{number} {name}' if number else name


def _merge(page, edit):
    by_id = {n['id']: n for n in page['nodes']}
    members = [by_id[i] for i in edit['nodeIds']]
    merged = _build_consolidated_node(edit['nodeIds'], by_id)
    name = edit.get('name') or ' + '.join(_label_name(n) for n in members)
    merged = {**merged, 'id': members[0]['id'], 'page': members[0].get('page', page['pageIndex']), 'label': _relabel(members[0], name),
              'mergedFrom': [{'id': n['id'], 'label': n['label']} for n in members]}
    if 'hazop' in merged:
        h = merged['hazop']
        reason = f"Merged by the reviewer: {', '.join(n['label'] for n in members)}" + (f" ({edit['reason']})" if edit.get('reason') else '')
        merged['hazop'] = {**h, 'name': name, 'justification': [*[j for j in h['justification'] if not j['reason'].startswith('Reduced option')],
                                                                  {'rule': 'S', 'reason': reason}]}
    gone = set(edit['nodeIds'][1:])
    out = []
    for n in page['nodes']:
        if n['id'] == members[0]['id']:
            out.append(merged)
        elif n['id'] not in gone:
            h = n.get('hazop')
            if h and h.get('typicalOf') in gone:  # typical of a node that was merged away: of the merged node now
                n = {**n, 'hazop': {**h, 'typicalOf': members[0]['id'], 'typicalOfName': name}}
            out.append(n)
    return {**page, 'nodes': out}


def apply_edits(document, edits):
    """The document with the edits applied, and the edits that no longer fit
    (nodes changed or gone since they were made)."""
    if not edits:
        return document, []
    pages = [dict(p) for p in document['pages']]
    stale = []
    for edit in edits:
        ids = edit['nodeIds'] if edit['type'] == 'merge' else [edit['nodeId']]
        page = next((p for p in pages if {n['id'] for n in p['nodes']} >= set(ids)), None)
        nodes = {n['id']: n for n in page['nodes']} if page else {}
        if not page or any(fingerprint(nodes[i]) != edit['fingerprints'].get(i) for i in ids):
            stale.append(edit)
            continue
        i = pages.index(page)
        if edit['type'] == 'merge':
            pages[i] = _merge(page, edit)
        elif edit['type'] == 'rename':
            pages[i] = {**page, 'nodes': [{**n, 'label': _relabel(n, edit['name']), **({'hazop': {**n['hazop'], 'name': edit['name']}} if n.get('hazop') else {})}
                                          if n['id'] == edit['nodeId'] else n for n in page['nodes']]}
    return {**document, 'pages': pages, 'edits': [e for e in edits if e not in stale]}, stale


def current_document(job_id, base=None):
    """The node breakdown as the viewer shows it: the base with the edits applied."""
    base = base or view_base(job_id)
    document, _ = apply_edits(jobs.read_artifact(job_id, base), load_edits(job_id, base))
    return document


def _find(document, node_id):
    for p in document['pages']:
        for n in p['nodes']:
            if n['id'] == node_id:
                return n
    return None


def _move_worksheets(study_id, moves):
    """Moves worksheet deviations: [(deviation ids, to node id, to label)]."""
    from ..worksheets import db_path
    path = db_path()
    if not path.exists():
        return
    with closing(sqlite3.connect(path)) as conn, conn:
        for dev_ids, node_id, label in moves:
            conn.executemany('UPDATE hazop_deviations SET node_id = ?, node_label = ? WHERE id = ?', [(node_id, label, d) for d in dev_ids])


def _deviations_of(study_id, node_ids):
    from ..worksheets import db_path
    path = db_path()
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path)) as conn:
        try:
            rows = conn.execute(f"SELECT id, node_id FROM hazop_deviations WHERE study_id = ? AND node_id IN ({','.join('?' * len(node_ids))})",
                                (study_id, *node_ids)).fetchall()
        except sqlite3.OperationalError:
            return {}
    out = {}
    for dev_id, node_id in rows:
        out.setdefault(node_id, []).append(dev_id)
    return out


def merge_nodes(job_id, node_ids, name='', reason='', base=None):
    """Merges nodes (first id survives). Returns (edit, document)."""
    base = base or view_base(job_id)
    document = current_document(job_id, base)
    node_ids = list(dict.fromkeys(node_ids))
    if len(node_ids) < 2:
        raise ValueError('name at least two different nodes to merge')
    missing = [i for i in node_ids if not _find(document, i)]
    if missing:
        raise ValueError(f"no node {', '.join(missing)} in this drawing's breakdown")
    pages = {next(p['pageIndex'] for p in document['pages'] if _find({'pages': [p]}, i)) for i in node_ids}
    if len(pages) > 1:
        raise ValueError('nodes on different pages cannot be merged')
    nodes = [_find(document, i) for i in node_ids]
    edit = {'id': uuid.uuid4().hex[:8], 'type': 'merge', 'nodeIds': node_ids, 'name': name.strip(), 'reason': reason.strip(),
            'fingerprints': {n['id']: fingerprint(n) for n in nodes}, 'at': jobs.now_iso(),
            'labels': [n['label'] for n in nodes]}
    moved = _deviations_of(job_id, node_ids[1:])
    edit['movedDeviations'] = moved
    edits = [*load_edits(job_id, base), edit]
    _save_edits(job_id, base, edits)
    document = current_document(job_id, base)
    target = _find(document, node_ids[0])
    _move_worksheets(job_id, [(ids, node_ids[0], target['label']) for ids in moved.values()])
    return edit, document


def rename_node(job_id, node_id, name, base=None):
    base = base or view_base(job_id)
    document = current_document(job_id, base)
    node = _find(document, node_id)
    if not node:
        raise ValueError(f'no node {node_id}')
    edit = {'id': uuid.uuid4().hex[:8], 'type': 'rename', 'nodeId': node_id, 'name': name.strip(),
            'fingerprints': {node_id: fingerprint(node)}, 'at': jobs.now_iso(), 'labels': [node['label']]}
    _save_edits(job_id, base, [*load_edits(job_id, base), edit])
    return edit, current_document(job_id, base)


def undo_last(job_id, base=None):
    """Removes the latest edit (moving merged worksheet rows back). Returns (edit or None, document)."""
    base = base or view_base(job_id)
    edits = load_edits(job_id, base)
    if not edits:
        return None, current_document(job_id, base)
    edit = edits[-1]
    _save_edits(job_id, base, edits[:-1])
    document = current_document(job_id, base)
    if edit['type'] == 'merge':
        _move_worksheets(job_id, [(ids, node_id, (_find(document, node_id) or {}).get('label', ''))
                                  for node_id, ids in edit.get('movedDeviations', {}).items()])
    return edit, document
