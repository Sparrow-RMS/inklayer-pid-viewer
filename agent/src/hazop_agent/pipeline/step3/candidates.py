"""Step 3 — candidate pair generation.

Deciding whether every pair of processes belongs in one HAZOP node is an
O(n^2) question, and asking a model to judge a pair with no real relation
wastes tokens and invites a spurious merge. This narrows the field to pairs
with a REAL reason to suspect they belong together, computed entirely from
step 1/2's already grounded data — no model call.

Each candidate carries its `reasons`, which are shown to the model (so its
judgment is grounded in why the pair was even asked about) and let a reviewer
audit a clustering decision afterward.
"""
from ..geometry import bbox_gap

# Two processes physically closer than this (page fractions) are worth asking
# about even with no other signal — HAZOP boundaries are sometimes purely
# spatial. Tight enough that "just somewhere on a dense sheet" doesn't qualify.
SPATIAL_ADJACENCY_GAP = 0.03


def generate_candidate_pairs(processes, entity_by_id):
    pairs = []
    for i in range(len(processes)):
        for j in range(i + 1, len(processes)):
            a, b = processes[i], processes[j]
            reasons = _pair_reasons(a, b, entity_by_id)
            if reasons:
                pairs.append({'id': f'c{len(pairs) + 1}', 'aId': a['id'], 'bId': b['id'], 'reasons': reasons})
    return pairs


def _connects(outlet_ids, target, by_id):
    """True if any entity in `outlet_ids` is directly connected (per step 1's
    resolved connectsTo graph) to any entity belonging to `target`, or sits
    among target's own entities/inlets."""
    target_entities = {*target['entityIds'], *target['inletEntityIds']}
    for outlet_id in outlet_ids or []:
        if outlet_id in target_entities:
            return True
        resolved = (by_id.get(outlet_id) or {}).get('resolvedConnections') or []
        if any(r in target_entities for r in resolved):
            return True
    return False


def _pair_reasons(a, b, entity_by_id):
    reasons = []

    b_entities = set(b['entityIds'])
    shared_entities = [eid for eid in a['entityIds'] if eid in b_entities]
    if shared_entities:
        reasons.append(f'shares {len(shared_entities)} entity/ies directly')

    a_primary = set(a['primaryEquipmentIds'])
    shared_primary = [eid for eid in b['primaryEquipmentIds'] if eid in a_primary]
    if shared_primary:
        reasons.append(f"shares major equipment ({', '.join(shared_primary)})")

    if b['id'] in ((a.get('flags') or {}).get('highOverlapWith') or []):
        reasons.append('flagged in step 2 as heavily overlapping')

    if _connects(a['outletEntityIds'], b, entity_by_id) or _connects(b['outletEntityIds'], a, entity_by_id):
        reasons.append("one's outlet connects to the other's piping")

    if not reasons and bbox_gap(a['bbox'], b['bbox']) <= SPATIAL_ADJACENCY_GAP:
        reasons.append('physically adjacent on the sheet')

    return reasons
