"""The HAZOP node-creation rules that can be computed exactly, with no model:

- facts the builder is given up front (control loops, relief devices and
  what they protect, off-page connectors, duplicate-train candidates), so it
  doesn't have to rediscover them from a long inventory;
- Step 7 validation (V1, V2, V4-V7, R6 and the Step 5 size check) run over
  whatever node breakdown the model proposed.

All pure.
"""
import re

from ..dsu import DisjointSet
from ..geometry import bbox_gap
from ..step1.extract import normalize_tag
from ..step2.classify import is_functional

# ISA loop: first letter (measured variable) + loop number, e.g. FT-202, FIC-202,
# FV-202. At least two letters, so interlock markers ("I026") don't qualify.
LOOP_TAG = re.compile(r'^([A-Z])[A-Z]{1,4}\s*-?\s*(\d{2,}[A-Z]?)$')
RELIEF_CATEGORIES = {'valve', 'fitting', 'instrument'}
# Members of one loop sit together on the sheet. Tag numbers are reused
# across unrelated devices (a "TV" T-valve and a "TIT" transmitter with the
# same number), so same-number items further apart than this are separate.
LOOP_REACH = 0.04
RELIEF_TAG = re.compile(r'^(PSV|PRV|PVRV|PVSV|TSV|BDV|RD|VB|BV|EV)\b', re.I)
RELIEF_WORDS = ('relief', 'safety valve', 'rupture', 'breather', 'vacuum', 'vent valve', 'flame arrest')
ATMOSPHERIC = re.compile(r'\bATM', re.I)
# Nearest equipment within this gap is taken as what a relief device protects,
# when the drawing's connections don't say.
RELIEF_REACH = 0.05
# Step 5 size limits.
MAX_INTERLOCKS_PER_NODE = 10
TARGET_NODES = (4, 5)
# More nodes than this means an operation has been split (Step 5).
MAX_NODES = 6
# Categories a node must cover (V1). Instruments and interlock markers are
# covered with the equipment they serve, so an orphan there matters less.
MUST_COVER = {'equipment', 'valve', 'line', 'connector'}
SHOULD_COVER = {'instrument', 'fitting', 'interlock_ref'}

TRAIN_KEYS = ('service', 'capacity', 'designPressure', 'designTemperature', 'material')

# Step 6a: which parameters a node's equipment requires, matched on the
# equipment's type text (and tag prefix for instruments).
EQUIPMENT_PARAMETERS = (
    (re.compile(r'tank|vessel|receiver|sump|\bpot\b|drum', re.I), ('level', 'pressure', 'temperature', 'composition')),
    (re.compile(r'pump|compressor|blower', re.I), ('flow', 'pressure')),
    (re.compile(r'filter|strainer', re.I), ('flow', 'pressure')),
    (re.compile(r'heater|cooler|exchanger|tracing|jacket|condenser|reboiler', re.I), ('temperature',)),
    (re.compile(r'agitator|mixer', re.I), ('mixing',)),
)
TEMPERATURE_INSTRUMENT = re.compile(r'^T[A-Z]{0,3}-?\d', re.I)
# Guidewords each parameter must have a deviation for; a tuple means any one of them.
REQUIRED_GUIDEWORDS = {
    'flow': ('NO', 'MORE', 'LESS', 'REVERSE'),
    'level': ('MORE', 'LESS'),
    'pressure': ('MORE', 'LESS'),
    'temperature': ('MORE', 'LESS'),
    'composition': (('OTHER THAN', 'AS WELL AS', 'PART OF'),),
    'mixing': (('NO', 'LESS'),),
}
# High/low protection is checked for these; composition and mixing have no "high".
PROTECTED_PARAMETERS = ('flow', 'level', 'pressure', 'temperature')

# Pressure ratings as printed -> bar(g).
_PRESSURE = re.compile(r'(-?\d+(?:\.\d+)?)\s*(kgf?\s*/\s*cm\s*\^?\s*2|bar|kpa|mpa|psi)', re.I)
_TO_BAR = {'kg': 0.980665, 'bar': 1.0, 'kpa': 0.01, 'mpa': 10.0, 'psi': 0.0689476}


def _attrs(e):
    return {a['key']: a['value'] for a in e.get('attributes') or []}


def control_loops(entities):
    """Instrument/valve groups sharing an ISA loop (first letter + number), or
    an explicit loopNumber attribute, that are also physically together.
    Only groups of two or more are loops."""
    loops = {}
    for e in entities:
        # Relief devices share loop numbers with the switches beside them but
        # aren't part of a loop; G3/G4 keep them with their equipment instead.
        if e['category'] not in ('instrument', 'valve') or not e.get('tag') or is_relief(e):
            continue
        explicit = _attrs(e).get('loopNumber')
        m = LOOP_TAG.match(e['tag'].upper().strip())
        key = f"{e['tag'].upper()[0]}-{explicit}" if explicit else (f'{m.group(1)}-{m.group(2)}' if m else None)
        if key:
            loops.setdefault(key, []).append(e)
    out = {}
    for key, members in loops.items():
        dsu = DisjointSet()
        for m in members:
            dsu.add(m['id'])
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if bbox_gap(a['bbox'], b['bbox']) <= LOOP_REACH:
                    dsu.union(a['id'], b['id'])
        groups = [g for g in dsu.groups() if len(g) >= 2]
        for n, group in enumerate(groups):
            out[key if len(groups) == 1 else f'{key} ({n + 1})'] = group
    return out


def is_relief(e):
    if e['category'] not in RELIEF_CATEGORIES:
        return False  # a relief LINE is the discharge path, not the device
    attrs = _attrs(e)
    text = f"{e.get('type', '')} {attrs.get('valveType', '')}".lower()
    return bool(RELIEF_TAG.match(e.get('tag') or '')) or any(w in text for w in RELIEF_WORDS)


def relief_devices(entities):
    """Each relief device with the equipment it protects: a drawn connection
    first, else the nearest equipment symbol."""
    equipment = [e for e in entities if e['category'] == 'equipment']
    by_id = {e['id']: e for e in entities}
    out = []
    for e in entities:
        if not is_relief(e):
            continue
        connected = [c for c in e.get('resolvedConnections') or [] if (by_id.get(c) or {}).get('category') == 'equipment']
        if not connected and equipment:
            nearest = min(equipment, key=lambda q: bbox_gap(q['bbox'], e['bbox']))
            if bbox_gap(nearest['bbox'], e['bbox']) <= RELIEF_REACH:
                connected = [nearest['id']]
        out.append({'id': e['id'], 'tag': e.get('tag'), 'setPressure': _attrs(e).get('setPressure'), 'protects': connected})
    return out


def duplicate_trains(entities):
    """Equipment that is probably a duplicate train (D1): identical data-block
    values under different tags, or an A/B spare pair."""
    groups = []
    by_signature = {}
    for e in entities:
        if e['category'] not in ('equipment', 'equipment_list_row') or not e.get('tag'):
            continue
        a = _attrs(e)
        signature = tuple(a.get(k, '').upper() for k in TRAIN_KEYS)
        if sum(bool(v) for v in signature) >= 3:
            by_signature.setdefault(signature, set()).add(e['tag'].upper())
    for sig, tags in by_signature.items():
        if len(tags) >= 2:
            groups.append({'tags': sorted(tags), 'why': 'identical data block (' + ', '.join(f'{k}={v}' for k, v in zip(TRAIN_KEYS, sig) if v) + ')'})
    stems = {}
    for e in entities:
        m = re.match(r'^(.*\d)([AB])$', (e.get('tag') or '').upper().strip())
        if m and e['category'] in ('equipment', 'equipment_list_row'):
            stems.setdefault(m.group(1), set()).add(m.group(0))
    groups += [{'tags': sorted(t), 'why': 'A/B spare pair'} for t in stems.values() if len(t) == 2]
    return groups


def connectors(entities):
    return [e for e in entities if e['category'] == 'connector']


def pressure_bar_g(text):
    """A printed pressure rating in bar(g): the highest positive value it
    gives, 0.0 for an atmospheric rating ("ATM", "ATM + LIQUID FILLED"), or
    None when it can't be read. Vacuum limits such as "-50 mm WC" are ignored."""
    text = text or ''
    values = []
    for number, unit in _PRESSURE.findall(text):
        unit = unit.lower()
        value = float(number) * _TO_BAR['kg' if unit.startswith('kg') else unit]
        if value > 0:
            values.append(value)
    if values:
        return max(values)
    return 0.0 if ATMOSPHERIC.search(text) else None


def _expand_ab(tag):
    """'F-V303A/B' -> ['F-V303A', 'F-V303B']; any other tag -> [tag]."""
    m = re.match(r'^(.*?)([A-Z])/([A-Z])$', (tag or '').upper().strip())
    return [m.group(1) + m.group(2), m.group(1) + m.group(3)] if m else [(tag or '').upper().strip()]


def pressure_ratings(entities):
    """Design pressure per equipment tag, in bar(g), from the symbol or its
    data block (an "A/B" data block rates both trains)."""
    out = {}
    for e in entities:
        if e['category'] not in ('equipment', 'equipment_list_row') or not e.get('tag'):
            continue
        printed = _attrs(e).get('designPressure')
        value = pressure_bar_g(printed)
        if value is None:
            continue
        for tag in _expand_ab(e['tag']):
            key = normalize_tag(tag)
            if key not in out or value > out[key]['bar']:
                out[key] = {'tag': tag, 'bar': value, 'printed': printed}
    return out


def _is_pump(e):
    return e['category'] == 'equipment' and re.search(r'pump|compressor|blower', e.get('type') or '', re.I)


def _rating_text(r):
    return r['printed'] if r['bar'] == 0 else f"{r['printed']} = {r['bar']:.2f} bar g"


def design_pressure_findings(nodes, entities):
    """V7: equipment rated below the highest pressure a source in the same
    node can produce (pump design pressure, relief set pressure)."""
    by_id = {e['id']: e for e in entities}
    ratings = pressure_ratings(entities)
    out = []
    for n in nodes:
        members = [by_id[i] for i in n['entityIds'] if i in by_id]
        sources = []
        for e in members:
            rating = ratings.get(normalize_tag(e.get('tag')))
            if _is_pump(e) and rating and rating['bar'] > 0:
                sources.append((rating['bar'], f"{e['tag']} design {rating['printed']}"))
            if is_relief(e):
                value = pressure_bar_g(_attrs(e).get('setPressure'))
                if value:
                    sources.append((value, f"{e.get('tag') or e['id']} set {_attrs(e)['setPressure']}"))
        if not sources:
            continue
        top, why = max(sources)
        low = {}
        for e in members:
            if e['category'] != 'equipment' or _is_pump(e) or not e.get('tag'):
                continue
            rating = ratings.get(normalize_tag(e['tag']))
            if rating and rating['bar'] < top - 1e-6:
                low[e['tag']] = rating
        if low:
            listed = ', '.join(f'{tag} ({_rating_text(r)})' for tag, r in sorted(low.items()))
            out.append(_finding('V7-design-pressure', 'warning',
                                f"{n['localId']} {n['name']}: rated below the {top:.1f} bar g a source in this node can produce ({why}): {listed}. "
                                'Study how they could be exposed (blocked discharge, reverse flow, relief return, cleaning path) as MORE pressure.',
                                node_ids=[n['localId']], entity_ids=[e['id'] for e in members if e.get('tag') in low]))
    return out


def required_parameters(members):
    """Step 6a: parameter -> why it is required, from the node's members."""
    out = {}
    for e in members:
        if e['category'] == 'line':
            out.setdefault('flow', 'lines')
        if e['category'] == 'equipment':
            for pattern, params in EQUIPMENT_PARAMETERS:
                if pattern.search(e.get('type') or ''):
                    for p in params:
                        out.setdefault(p, e.get('tag') or e['type'])
        if e['category'] == 'instrument' and TEMPERATURE_INSTRUMENT.match(e.get('tag') or ''):
            out.setdefault('temperature', e['tag'])
    return out


def _guideword(g):
    g = (g or '').upper().strip()
    return {'HIGH': 'MORE', 'HIGHER': 'MORE', 'LOW': 'LESS', 'LOWER': 'LESS', 'MISDIRECTED': 'OTHER THAN'}.get(g, g)


def parameter_coverage_findings(nodes, entities):
    """V6: every parameter the node's equipment requires has a deviation for
    each required guideword."""
    by_id = {e['id']: e for e in entities}
    out = []
    for n in nodes:
        # A typical node (D2) lists only its differences; its full node is studied.
        if n.get('typicalOf'):
            continue
        required = required_parameters([by_id[i] for i in n['entityIds'] if i in by_id])
        have = {(d['parameter'], _guideword(d['guideword'])) for d in n['keyDeviations']}
        missing = []
        for param in required:
            for need in REQUIRED_GUIDEWORDS.get(param, ()):
                options = need if isinstance(need, tuple) else (need,)
                if not any((param, g) in have for g in options):
                    missing.append(f"{'/'.join(options)} {param}")
        if missing:
            out.append(_finding('V6-parameter-coverage', 'warning',
                                f"{n['localId']} {n['name']}: no deviation for {', '.join(missing)} "
                                f"(required by {', '.join(f'{p}: {w}' for p, w in required.items())}).",
                                node_ids=[n['localId']]))
    return out


def canonical_parameter(text):
    """Free-text parameter (old node format) -> a Step 6a parameter."""
    t = (text or '').lower()
    for param, words in (('pressure', ('pressure', 'vacuum')), ('temperature', ('temperature', 'temp')),
                         ('level', ('level', 'inventory', 'overfill')), ('mixing', ('agitation', 'mixing')),
                         ('composition', ('composition', 'material', 'concentration', 'contamination')),
                         ('flow', ('flow', 'path', 'routing', 'delivery', 'unloading')), ('phase', ('phase',)),
                         ('reaction', ('reaction',))):
        if any(w in t for w in words):
            return param
    return 'other'


def _consequences_of(c):
    """A cause's consequences; a cause carrying only safeguards gets them under
    one consequence still to be described."""
    if 'consequences' in c:
        return c['consequences']
    return [{'consequence': '', 'safeguards': c.get('safeguards') or []}] if c.get('safeguards') else []


def _causes_of(d):
    """A deviation's causes, each with its consequences; older results had one
    'credibleCause' sentence with the deviation's safeguards."""
    if 'causes' in d:
        return [{'cause': c['cause'], 'tags': c.get('tags') or [], 'consequences': _consequences_of(c)} for c in d['causes']]
    return [{'cause': d.get('credibleCause') or '', 'tags': [], 'consequences': _consequences_of(d)}]


def normalize_hazop_fields(n):
    """Reads a node in an older format as the current one: the single-parameter
    format ('protection' with one criticalParameter, deviations without
    safeguards), deviations with one 'credibleCause' instead of a list of
    causes, and causes without consequences. Nodes already in the current
    format are returned unchanged."""
    if 'parameters' in n:
        if all('consequences' in c for d in n.get('keyDeviations') or [] for c in d.get('causes', [{}])):
            return n
        return {**n, 'keyDeviations': [{**{k: v for k, v in d.items() if k not in ('credibleCause', 'safeguards')}, 'causes': _causes_of(d)}
                                       for d in n['keyDeviations']]}
    p = n.get('protection') or {}
    critical = p.get('criticalParameter') or ''
    out = {k: v for k, v in n.items() if k != 'protection'}
    out['parameters'] = [{'parameter': canonical_parameter(critical), 'name': critical,
                          'highSafeguards': p.get('highSafeguards') or [], 'lowSafeguards': p.get('lowSafeguards') or []}] if p else []
    out['vacuumProtection'] = p.get('vacuumProtection')
    out['keyDeviations'] = [{'guideword': _guideword(d['guideword']), 'parameter': canonical_parameter(d['parameter']), 'detail': d['parameter'],
                             'causes': _causes_of(d)}
                            for d in n.get('keyDeviations') or []]
    return out


# ──────────────────────────────────────────────────────────────── validation

def _finding(check, severity, detail, node_ids=(), entity_ids=(), refs=()):
    return {'check': check, 'severity': severity, 'detail': detail,
            'nodeIds': list(node_ids), 'entityIds': list(entity_ids), 'interlockRefs': list(refs)}


def _label(e):
    return e.get('tag') or f"{e['category']} {e['id']}"


def validate(nodes, entities, interlocks, revised_ids, modes=(), out_of_scope=()):
    """Step 7 over a proposed breakdown. `nodes` carry `localId`, `entityIds`
    (already expanded from their processes), `interlockRefs`, `parameters`,
    `vacuumProtection`, `keyDeviations`, `boundaryFrom/To`, `revisedScope`, `operatingModes`, `equipmentTags`.
    `out_of_scope` are entity ids declared as only passing across the sheet
    (O5); they count as covered."""
    by_id = {e['id']: e for e in entities}
    owners = {}
    for n in nodes:
        for eid in n['entityIds']:
            owners.setdefault(eid, []).append(n['localId'])
    out = []

    # V1 — coverage: every functional item in exactly one node. A tag named
    # as a node boundary may legitimately sit in both nodes it separates.
    boundary_tags = {normalize_tag(b['tag']) for n in nodes for b in (*n['boundaryFrom'], *n['boundaryTo'])}
    for severity, categories in (('error', MUST_COVER), ('warning', SHOULD_COVER)):
        orphans = [e for e in entities if e['category'] in categories and is_functional(e) and e['id'] not in owners and e['id'] not in out_of_scope]
        if orphans:
            out.append(_finding('V1-orphan', severity, f"{len(orphans)} {'/'.join(sorted(categories))} item(s) in no node: "
                                + ', '.join(_label(e) for e in orphans[:25]) + (' …' if len(orphans) > 25 else ''),
                                entity_ids=[e['id'] for e in orphans]))
    shared = [eid for eid, ns in owners.items() if len(ns) > 1 and normalize_tag(by_id.get(eid, {}).get('tag')) not in boundary_tags
              and by_id.get(eid, {}).get('category') in MUST_COVER]
    if shared:
        out.append(_finding('V1-shared', 'warning', f"{len(shared)} item(s) in more than one node without being a boundary: "
                            + ', '.join(f"{_label(by_id[e])} ({'/'.join(owners[e])})" for e in shared[:15]), entity_ids=shared))

    # V2 — every C&E row assigned to exactly one node.
    table_refs = {normalize_tag(r['ref']): r['ref'] for r in interlocks}
    assigned = {}
    for n in nodes:
        for ref in n['interlockRefs']:
            assigned.setdefault(normalize_tag(ref), []).append(n['localId'])
    unassigned = [ref for key, ref in table_refs.items() if key not in assigned]
    if unassigned:
        out.append(_finding('V2-unassigned', 'error', f"C&E rows in no node: {', '.join(unassigned)}", refs=unassigned))
    for key, ns in assigned.items():
        if len(ns) > 1:
            out.append(_finding('V2-duplicate', 'error', f"C&E row {table_refs.get(key, key)} assigned to {', '.join(ns)}; it belongs to the node of its final action.",
                                node_ids=ns, refs=[table_refs.get(key, key)]))
        if table_refs and key not in table_refs:
            out.append(_finding('V2-unknown', 'warning', f"Node(s) {', '.join(ns)} cite interlock {key}, which is not in the C&E table.", node_ids=ns, refs=[key]))

    # V4 — a safeguard against HIGH and LOW of every parameter the node lists,
    # and vacuum protection on atmospheric tanks.
    for n in nodes:
        gaps = [f"{side} {p['name'] or p['parameter']}" for p in n['parameters'] if p['parameter'] in PROTECTED_PARAMETERS
                for side, tags in (('HIGH', p['highSafeguards']), ('LOW', p['lowSafeguards'])) if not tags]
        if gaps:
            out.append(_finding('V4-protection-gap', 'warning', f"{n['localId']} {n['name']}: no safeguard against {', '.join(gaps)}.",
                                node_ids=[n['localId']]))
        atmospheric = [e for e in (by_id.get(eid) for eid in n['entityIds'])
                       if e and e['category'] == 'equipment' and ATMOSPHERIC.search(_attrs(e).get('designPressure', ''))]
        if (n['vacuumProtection'] or '').strip().upper() in ('NONE SHOWN', 'NONE') or (atmospheric and not n['vacuumProtection']):
            out.append(_finding('V4-vacuum', 'warning', f"{n['localId']} {n['name']}: atmospheric tank with no vacuum protection shown.", node_ids=[n['localId']]))

    # V5 — boundary sanity.
    owner_of = {eid: set(ns) for eid, ns in owners.items()}
    for loop, members in control_loops(entities).items():
        homes = set().union(*(owner_of.get(m, set()) for m in members))
        if len(homes) > 1:
            out.append(_finding('V5-split-loop', 'error', f"Control loop {loop} ({', '.join(_label(by_id[m]) for m in members)}) is split across {', '.join(sorted(homes))}.",
                                node_ids=sorted(homes), entity_ids=members))
    for relief in relief_devices(entities):
        mine = owner_of.get(relief['id'], set())
        for eq in relief['protects']:
            if mine and owner_of.get(eq) and not mine & owner_of[eq]:
                out.append(_finding('V5-relief-separated', 'error', f"{relief['tag'] or relief['id']} is not in the node of the equipment it protects ({_label(by_id[eq])}).",
                                    node_ids=sorted(mine | owner_of[eq]), entity_ids=[relief['id'], eq]))
    for n in nodes:
        mid_line = [b['tag'] for b in (*n['boundaryFrom'], *n['boundaryTo']) if b['kind'] == 'line']
        if mid_line:
            out.append(_finding('V5-mid-line', 'warning', f"{n['localId']} {n['name']}: boundary on a line ({', '.join(mid_line)}); place it at a valve, flange or nozzle.",
                                node_ids=[n['localId']]))

    # V6 — every required parameter x guideword has a deviation; V7 — design
    # pressure below what a source in the same node can produce.
    out += parameter_coverage_findings(nodes, entities)
    out += design_pressure_findings(nodes, entities)

    # R6 — new/modified scope is its own node, never merged into existing scope.
    for n in nodes:
        functional = [eid for eid in n['entityIds'] if by_id.get(eid) and by_id[eid]['category'] in MUST_COVER | SHOULD_COVER]
        revised_here = [eid for eid in functional if eid in revised_ids]
        if revised_here and not n['revisedScope'] and len(revised_here) >= 2:
            out.append(_finding('R6-merged', 'error', f"{n['localId']} {n['name']} contains new/modified (red) items but is not a separate revised-scope node: "
                                + ', '.join(_label(by_id[e]) for e in revised_here[:10]), node_ids=[n['localId']], entity_ids=revised_here))
        if n['revisedScope'] and functional and not revised_here:
            out.append(_finding('R6-flag', 'warning', f"{n['localId']} {n['name']} is marked revised scope but contains no red items.", node_ids=[n['localId']]))
        # The reverse of R6-merged: existing equipment pulled into a revised node.
        # Existing items stay in their own node, which gains the new operating mode.
        existing = [eid for eid in n['entityIds'] if eid not in revised_ids and by_id.get(eid, {}).get('category') == 'equipment']
        if n['revisedScope'] and existing:
            out.append(_finding('R6-existing-in-revised', 'error', f"{n['localId']} {n['name']} is marked new/modified scope but contains existing equipment: "
                                + ', '.join(_label(by_id[e]) for e in existing[:10]) + '. Keep existing items in their own node (add the new operating mode '
                                'there) and put only the new items, with their tie-in valves, in the R6 node.', node_ids=[n['localId']], entity_ids=existing))

    # Step 5 — size.
    # SPLIT is a rule, not advice: too many interlocks, or two of the C&E
    # table's operating modes (e.g. FILLING and TRANSFER) in one node. Modes
    # count only if the table names them.
    table_modes = {m.upper().replace(' ', '') for m in modes}
    for n in nodes:
        if len(n['interlockRefs']) > MAX_INTERLOCKS_PER_NODE:
            out.append(_finding('S-too-many-interlocks', 'warning', f"{n['localId']} {n['name']} carries {len(n['interlockRefs'])} interlocks (~10 or fewer is usual); check it is one operation.", node_ids=[n['localId']]))
        node_modes = [m for m in n['operatingModes'] if m.upper().replace(' ', '') in table_modes]
        if len(node_modes) > 1:
            out.append(_finding('S-two-modes', 'error', f"{n['localId']} {n['name']} spans operating modes {', '.join(node_modes)}; split it.", node_ids=[n['localId']]))
    lo, hi = TARGET_NODES
    if len(nodes) > MAX_NODES:
        out.append(_finding('S-node-count', 'warning', f"{len(nodes)} nodes on this sheet (target {lo}-{hi}); an operation has probably been split, or an inlet/outlet made its own node."))
    elif nodes and not lo <= len(nodes) <= hi:
        out.append(_finding('S-node-count', 'info', f"{len(nodes)} nodes on this sheet (default target {lo}-{hi})."))
    return out
