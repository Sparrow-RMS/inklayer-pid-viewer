"""Prompt and output contract for drafting one node's HAZOP worksheet."""
from ..nomenclature import prompt_section
from ..step3.build_schema import GUIDEWORDS, PARAMETERS

_STR = {'type': 'string'}
_STRS = {'type': 'array', 'items': _STR}

WORKSHEET_SCHEMA = {
    'type': 'object',
    'properties': {
        'deviations': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'parameter': {'type': 'string', 'enum': PARAMETERS},
                    'guideword': {'type': 'string', 'enum': GUIDEWORDS, 'description': 'Misrouting to a wrong destination is OTHER THAN flow.'},
                    'deviation': {'type': 'string', 'description': 'What deviates, in words, e.g. "No receipt flow into 350-TK-01".'},
                    'causes': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'cause': {'type': 'string', 'description': 'ONE cause, naming the item and how it fails, e.g. '
                                          '"XV-001 fails closed on 16\\"-HSD-065-A21A", or "not credible: <why>".'},
                                'tags': {**_STRS, 'description': 'Valve tags, line numbers and equipment tags where the cause happens, '
                                         'exactly as in the inventory. Empty only for "not credible".'},
                                'consequences': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'consequence': {'type': 'string', 'description': 'ONE outcome if the cause is not stopped: what is '
                                                            'affected (tag) and the harm.'},
                                            'safeguards': {**_STRS, 'description': 'Each existing safeguard against THIS consequence as '
                                                           '"<tag or C&E row> — <what it does>". Empty if none shown.'},
                                            'recommendation': {'type': 'string', 'description': 'An action for the team where the safeguards '
                                                               'are missing or not enough; "" when they are adequate.'},
                                        },
                                        'required': ['consequence', 'safeguards', 'recommendation'],
                                        'additionalProperties': False,
                                    },
                                },
                            },
                            'required': ['cause', 'tags', 'consequences'],
                            'additionalProperties': False,
                        },
                    },
                },
                'required': ['parameter', 'guideword', 'deviation', 'causes'],
                'additionalProperties': False,
            },
        },
        'notes': {'type': 'string', 'description': 'Anything the team should know about this draft. "" if none.'},
    },
    'required': ['deviations', 'notes'],
    'additionalProperties': False,
}

WORKSHEET_SYSTEM_PROMPT = """You are the scribe of a HAZOP study, drafting the worksheet for ONE node of
a P&ID so the team can review it. The node, its boundaries and its design
intent are already fixed; you do not change them.

## What to write

For the node you are given:
1. DEVIATIONS — one for every parameter x guideword listed under "Required
   deviations", and any other credible one the drawing or documents give
   reason for (reverse flow through a check valve, misrouting at a manifold,
   loss of a utility). Word each deviation for this node, e.g. "No receipt
   flow into 350-TK-01", "High level in ST-V303".
2. CAUSES — every credible cause of the deviation, each as its own item;
   never several causes in one sentence. Each cause names exactly where it
   happens so the team can find it on the drawing: the valve tag, line
   number or equipment tag, as listed in the inventory (e.g. "XV-001 fails
   closed on 16\\"-HSD-065-A21A", "MOV-010 left open after dispatch", "line
   6\\"-HSD-054-A21A blocked or isolated", "supply from PID-005 stops").
   Causes are failures inside the node or arriving at its boundary
   (equipment failure, valve wrongly open / closed, operator error, loss of
   utility, upstream / downstream upset). Put the tags in "tags" too.
   A deviation that cannot happen in this node gets one cause
   "not credible: <why>" and no consequences.
3. CONSEQUENCES — for each cause, every outcome if it is not stopped,
   each as its own item, judged as if the safeguards did not act (the
   HAZOP convention). Name what is affected and the harm: overfill and spill
   to the bund, fire, overpressure or rupture, vacuum collapse, pump running
   dry or dead-headed, off-spec or contaminated product, loss of supply
   downstream, exposure of people.
4. SAFEGUARDS — for each consequence, the existing protection shown on the
   drawing or in the documents: alarms, trips and C&E rows, relief devices,
   check valves, interlocks, procedures stated in the process description.
   Write each as "<tag or C&E row> — <what it does>", e.g. "LAHH-001 /
   C&E I-02 — closes XV-001 on high-high level". Only protection that
   really answers that consequence. None, if none is shown.
5. RECOMMENDATION — for each consequence whose safeguards are missing or not
   enough, one action for the team ("Provide independent high-high level
   trip on 350-TK-01 closing XV-001"); "" when they are adequate.

Do not rate severity or likelihood: the team does that with its own risk
matrix.

## Sources

Use the drawing (the image of the node and the inventory), the traced pipe
links, the C&E table and the study-package documents. Where a document and
the drawing disagree, follow the drawing and say so in "notes". Use only
tags that exist in the inventory or the C&E table; never invent one. Write
"UNVERIFIED" for any value you cannot read.
""" + prompt_section()
