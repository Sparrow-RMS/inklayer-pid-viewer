"""Step 3 output contracts: pairwise same-node verdicts, and the continuous
relatedness scores the consolidation pass uses.

Copied verbatim from server/src/pipeline/step3/schema.js.
"""

PAIR_VERDICT_SCHEMA = {'type': 'object',
 'properties': {'verdicts': {'type': 'array',
                             'items': {'type': 'object',
                                       'properties': {'pairId': {'type': 'string',
                                                                 'description': 'The id of the candidate '
                                                                                'pair being judged, exactly '
                                                                                'as given.'},
                                                      'sameNode': {'type': 'boolean',
                                                                   'description': 'true if these two '
                                                                                  'processes belong in one '
                                                                                  'HAZOP node, false if they '
                                                                                  'should stay separate '
                                                                                  'nodes.'},
                                                      'reasoning': {'type': 'string',
                                                                    'description': 'One or two sentences: '
                                                                                   'the HAZOP boundary logic '
                                                                                   'behind this verdict.'},
                                                      'confidence': {'type': 'number',
                                                                     'description': '0-1.'}},
                                       'required': ['pairId', 'sameNode', 'reasoning', 'confidence'],
                                       'additionalProperties': False}}},
 'required': ['verdicts'],
 'additionalProperties': False}

RELATEDNESS_SCHEMA = {'type': 'object',
 'properties': {'scores': {'type': 'array',
                           'items': {'type': 'object',
                                     'properties': {'pairId': {'type': 'string',
                                                               'description': 'The id of the pair being '
                                                                              'scored, exactly as given.'},
                                                    'relatedness': {'type': 'number',
                                                                    'description': '0-1: how strongly these '
                                                                                   'two nodes belong under '
                                                                                   'one broader review node. '
                                                                                   '0 = no reason to ever '
                                                                                   'combine, 1 = obviously '
                                                                                   'one scope.'},
                                                    'reasoning': {'type': 'string',
                                                                  'description': 'One or two sentences '
                                                                                 'grounding the score in '
                                                                                 'real equipment/piping.'}},
                                     'required': ['pairId', 'relatedness', 'reasoning'],
                                     'additionalProperties': False}}},
 'required': ['scores'],
 'additionalProperties': False}
