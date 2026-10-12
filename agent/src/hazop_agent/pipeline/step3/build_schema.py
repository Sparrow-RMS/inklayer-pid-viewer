"""Output contract for the rules-driven node builder (HAZOP node-creation
rules, Step 6). A node references step 2 process ids and step 1 entity ids
only; its geometry is derived from them, never from the model."""

RULE_IDS = ['O1', 'O2', 'O3', 'O4', 'O5', 'R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'D1', 'D2', 'D3', 'D4', 'D5', 'S']
# Step 6a. "mixing" is agitation; "composition" covers wrong material and contamination.
PARAMETERS = ['flow', 'level', 'pressure', 'temperature', 'composition', 'mixing', 'phase', 'reaction', 'other']
GUIDEWORDS = ['NO', 'MORE', 'LESS', 'REVERSE', 'AS WELL AS', 'PART OF', 'OTHER THAN', 'EARLY', 'LATE', 'BEFORE', 'AFTER']
BOUNDARY_KINDS = ['valve', 'flange', 'nozzle', 'connector', 'equipment', 'line', 'other']
SHEET_PATTERNS = ['P1', 'P2', 'P3']
FINDING_CHECKS = ['tag-not-on-drawing', 'tag-not-in-table', 'open-and-closed', 'permissive-trip-mismatch',
                  'copy-paste', 'unprotected-new-scope', 'protection-gap', 'other']

_STR = {'type': 'string'}
_STRS = {'type': 'array', 'items': _STR}

_BOUNDARY = {
    'type': 'array',
    'items': {
        'type': 'object',
        'properties': {
            'tag': {'type': 'string', 'description': 'Exact tag / line number / connector as printed, or "UNVERIFIED".'},
            'kind': {'type': 'string', 'enum': BOUNDARY_KINDS},
            'note': {'type': 'string', 'description': 'e.g. "discharge flange", "continues on 44AB0500-...". "" if none.'},
        },
        'required': ['tag', 'kind', 'note'],
        'additionalProperties': False,
    },
}

NODE_BUILD_SCHEMA = {
    'type': 'object',
    'properties': {
        'sheetPatterns': {
            'type': 'array',
            'items': {'type': 'string', 'enum': SHEET_PATTERNS},
            'description': 'Step 0: which pattern(s) this sheet shows. P1 receive->hold, P2 hold->distribute, P3 receive->process->distribute.',
        },
        'nodes': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'localId': {'type': 'string', 'description': '"N1", "N2", ... in review order.'},
                    'name': {'type': 'string', 'description': 'Short and functional, e.g. "Transfer pumps P-V303A/B".'},
                    'processIds': {**_STRS, 'description': 'Step 2 process ids wholly inside this node.'},
                    'entityIds': {**_STRS, 'description': 'Extra entity ids that belong here but are in no listed process, or whose process spans a boundary.'},
                    'boundaryFrom': _BOUNDARY,
                    'boundaryTo': _BOUNDARY,
                    'equipmentTags': _STRS,
                    'designIntent': {'type': 'string', 'description': 'One sentence: "To <verb> <material> from <X> to <Y> at <P, T, flow>".'},
                    'designConditions': {
                        'type': 'object',
                        'properties': {
                            'pressure': _STR, 'temperature': _STR, 'material': _STR,
                            'source': {'type': 'string', 'description': 'Where the values come from, e.g. "ST-V303 data block". "UNVERIFIED" values stay "UNVERIFIED".'},
                        },
                        'required': ['pressure', 'temperature', 'material', 'source'],
                        'additionalProperties': False,
                    },
                    'controlLoops': {**_STRS, 'description': 'Loop tags, e.g. "WIT-1257001".'},
                    'interlockRefs': {**_STRS, 'description': 'C&E row refs whose FINAL ACTION happens in this node.'},
                    'reliefDevices': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {'tag': _STR, 'setPressure': {'type': 'string', 'description': 'As printed, or "UNVERIFIED".'}},
                            'required': ['tag', 'setPressure'],
                            'additionalProperties': False,
                        },
                    },
                    'parameters': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'parameter': {'type': 'string', 'enum': PARAMETERS},
                                'name': {'type': 'string', 'description': 'What exactly, e.g. "ST-V303 level", "filter differential pressure".'},
                                'highSafeguards': {**_STRS, 'description': 'Tags / C&E rows protecting against HIGH.'},
                                'lowSafeguards': {**_STRS, 'description': 'Tags / C&E rows protecting against LOW.'},
                            },
                            'required': ['parameter', 'name', 'highSafeguards', 'lowSafeguards'],
                            'additionalProperties': False,
                        },
                        'description': 'EVERY parameter that applies to the node (Step 6a), not only the critical one.',
                    },
                    'vacuumProtection': {'type': ['string', 'null'], 'description': 'For an atmospheric tank: the vent / breather / VB tag, or "NONE SHOWN". Null if no atmospheric tank.'},
                    'keyDeviations': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'guideword': {'type': 'string', 'enum': GUIDEWORDS, 'description': 'Misrouting to a wrong destination is OTHER THAN flow.'},
                                'parameter': {'type': 'string', 'enum': PARAMETERS},
                                'detail': {'type': 'string', 'description': 'What deviates, e.g. "flow into ST-V303", "filter differential pressure".'},
                                'credibleCause': {'type': 'string', 'description': 'The cause in this node, or "not credible: <why>".'},
                                'safeguards': {**_STRS, 'description': 'Tags / C&E rows that answer this deviation. Empty if none shown.'},
                            },
                            'required': ['guideword', 'parameter', 'detail', 'credibleCause', 'safeguards'],
                            'additionalProperties': False,
                        },
                        'description': 'For EVERY applicable parameter, each required guideword (Step 6a).',
                    },
                    'operatingModes': {**_STRS, 'description': 'C&E modes this node covers.'},
                    'utilitiesAsSafeguards': {**_STRS, 'description': 'Utilities entering the node, reviewed as loss of utility (G5).'},
                    'typicalOf': {'type': ['string', 'null'], 'description': 'localId of the node this duplicates (D2). Null if not typical.'},
                    'differencesFromTypical': {**_STRS, 'description': 'For a typical node: ONLY the differences (tags, shared headers, interlock numbers).'},
                    'revisedScope': {'type': 'boolean', 'description': 'true if this node is the new/modified (red) scope (R6).'},
                    'justification': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {'rule': {'type': 'string', 'enum': RULE_IDS}, 'reason': _STR},
                            'required': ['rule', 'reason'],
                            'additionalProperties': False,
                        },
                    },
                    'unverified': {**_STRS, 'description': 'Every value in this node marked UNVERIFIED, with what it is.'},
                },
                'required': ['localId', 'name', 'processIds', 'entityIds', 'boundaryFrom', 'boundaryTo', 'equipmentTags',
                             'designIntent', 'designConditions', 'controlLoops', 'interlockRefs', 'reliefDevices', 'parameters',
                             'vacuumProtection', 'keyDeviations', 'operatingModes', 'utilitiesAsSafeguards', 'typicalOf', 'differencesFromTypical',
                             'revisedScope', 'justification', 'unverified'],
                'additionalProperties': False,
            },
        },
        'consistencyFindings': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'check': {'type': 'string', 'enum': FINDING_CHECKS},
                    'interlockRefs': _STRS,
                    'tags': _STRS,
                    'detail': _STR,
                },
                'required': ['check', 'interlockRefs', 'tags', 'detail'],
                'additionalProperties': False,
            },
            'description': 'V3/V4 findings beyond the ones already computed and listed for you.',
        },
        'outOfScope': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'entityIds': _STRS,
                    'reason': {'type': 'string', 'description': 'e.g. "IA header passing between two connectors, serves nothing on this sheet".'},
                },
                'required': ['entityIds', 'reason'],
                'additionalProperties': False,
            },
            'description': 'O5: items in no node because they only pass across the sheet. Never the inlets/outlets of a node (those go in it, O2).',
        },
        'notes': {'type': 'string', 'description': 'Anything a reviewer should know about this breakdown. "" if none.'},
    },
    'required': ['sheetPatterns', 'nodes', 'consistencyFindings', 'outOfScope', 'notes'],
    'additionalProperties': False,
}
