"""Output contract for the cause-and-effect / interlock table transcription.

Strict JSON-schema subset, as in the other steps: every property required,
no additional properties.
"""

INTERLOCK_KINDS = ['trip', 'permissive', 'auto-close', 'auto-open', 'alarm', 'other']
REQUIRED_STATES = ['open', 'closed', 'running', 'stopped', 'not-started', 'inline', 'other']

_STRING_LIST = {'type': 'array', 'items': {'type': 'string'}}

INTERLOCK_TABLE_SCHEMA = {
    'type': 'object',
    'properties': {
        'tablePresent': {'type': 'boolean', 'description': 'false if the sheet has no cause-and-effect / interlock table.'},
        'tableTitle': {'type': 'string', 'description': 'Heading of the table exactly as printed, or "".'},
        'modes': {**_STRING_LIST, 'description': 'Operating-mode headings in table order, e.g. "ST-V305 FILLING MODE".'},
        'interlocks': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'ref': {'type': 'string', 'description': 'The row number exactly as printed, e.g. "1257022". "UNVERIFIED" if unreadable.'},
                    'mode': {'type': ['string', 'null'], 'description': 'The mode heading this row sits under, exactly as listed in "modes". Null if the table has no modes.'},
                    'kind': {'type': 'string', 'enum': INTERLOCK_KINDS},
                    'text': {'type': 'string', 'description': 'The full row text, verbatim, with line breaks joined by spaces.'},
                    'causeTags': {**_STRING_LIST, 'description': 'Tags whose state or reading triggers or conditions this row (transmitters, switches, valve positions).'},
                    'finalElementTags': {**_STRING_LIST, 'description': 'The devices that ACT: the valves that close or open, the pump that trips or is blocked from starting.'},
                    'requiredStates': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'tag': {'type': 'string'},
                                'state': {'type': 'string', 'enum': REQUIRED_STATES},
                            },
                            'required': ['tag', 'state'],
                            'additionalProperties': False,
                        },
                        'description': 'For a permissive or position check: every tag with the state the row requires, one entry per mention. Record contradictions as printed, never resolve them.',
                    },
                    'markedRevised': {'type': 'boolean', 'description': 'true if the row is printed in red or clouded (marked "(red)" in the text listing).'},
                    'unverified': {'type': 'boolean', 'description': 'true if any tag or value in this row is illegible or ambiguous.'},
                    'unverifiedReason': {'type': 'string', 'description': 'What could not be read, or "".'},
                },
                'required': ['ref', 'mode', 'kind', 'text', 'causeTags', 'finalElementTags', 'requiredStates', 'markedRevised', 'unverified', 'unverifiedReason'],
                'additionalProperties': False,
            },
        },
        'notes': {'type': 'string', 'description': 'Anything about the table worth flagging: unreadable rows, rows cut off, numbering gaps. "" if none.'},
    },
    'required': ['tablePresent', 'tableTitle', 'modes', 'interlocks', 'notes'],
    'additionalProperties': False,
}
