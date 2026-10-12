"""The P&ID legend knowledge base (built by docs/build_nomenclature.py from a
legend sheet), for prompts and for exact lookups in code."""
import json
import re
from functools import lru_cache
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / 'docs'

# ISA 5.1 letters, used when a code is not in the legend's own table.
ISA_FIRST = {
    'A': 'analysis', 'B': 'burner / combustion', 'C': 'conductivity', 'D': 'density', 'E': 'voltage', 'F': 'flow',
    'G': 'gauging / position', 'H': 'hand', 'I': 'current', 'J': 'power', 'K': 'time', 'L': 'level', 'M': 'moisture',
    'P': 'pressure', 'Q': 'quantity', 'R': 'radiation', 'S': 'speed', 'T': 'temperature', 'U': 'multivariable',
    'V': 'vibration', 'W': 'weight / force', 'X': 'unclassified (on/off)', 'Y': 'event / state', 'Z': 'position',
}
ISA_MODIFIER = {'D': 'differential', 'F': 'ratio', 'Q': 'totalizing', 'J': 'scanning'}
ISA_FUNCTION = {
    'A': 'alarm', 'C': 'controller', 'E': 'element', 'G': 'gauge / glass', 'I': 'indicator', 'K': 'control station',
    'L': 'light', 'O': 'orifice', 'P': 'test point', 'R': 'recorder', 'S': 'switch', 'T': 'transmitter', 'V': 'valve',
    'W': 'well', 'X': 'unclassified', 'Y': 'relay / compute', 'Z': 'final element / actuator',
}
ISA_LEVEL = {'HH': 'high-high', 'LL': 'low-low', 'H': 'high', 'L': 'low', 'M': 'middle'}
# Common codes that letter-by-letter ISA reading gets wrong.
OVERRIDES = {
    'PSV': 'pressure safety valve', 'PRV': 'pressure relief valve', 'TSV': 'thermal safety valve',
    'PVSV': 'pressure / vacuum safety valve', 'PVRV': 'pressure / vacuum relief valve', 'VB': 'vacuum breaker',
    'BDV': 'blowdown valve', 'XV': 'on/off valve', 'HV': 'hand-operated valve', 'SDV': 'shutdown valve',
    'ESDV': 'emergency shutdown valve', 'MOV': 'motor-operated valve', 'RD': 'rupture disc',
    'AFR': 'air filter regulator',
}
_PREFIX = re.compile(r'^([A-Z]{1,6})\d*\s*-?')


@lru_cache(maxsize=1)
def legend():
    path = DOCS / 'nomenclature.json'
    return json.loads(path.read_text()) if path.exists() else {'sections': {}}


@lru_cache(maxsize=1)
def reference_text():
    """The legend, compact, for prompts ('' when none has been built)."""
    path = DOCS / 'nomenclature.md'
    return path.read_text() if path.exists() else ''


@lru_cache(maxsize=1)
def _instrument_codes():
    rows = legend()['sections'].get('Instruments Identification', {}).get('entries', [])
    return {r['code'].upper(): r['meaning'] for r in rows}


def decode_instrument(tag):
    """'LSHH-1257001' -> 'level switch high-high'; None when the prefix isn't
    an instrument code. The legend's own table wins over generic ISA letters.
    Callers pass instrument tags only: 'ST-V303' would read as an instrument."""
    m = _PREFIX.match((tag or '').upper().strip())
    if not m:
        return None
    code = m.group(1)
    known = _instrument_codes()
    if code in known:
        return known[code].lower()
    if code in OVERRIDES:
        return OVERRIDES[code]
    if code.startswith('DP') and len(code) > 2:
        # DP... is written for differential pressure (ISA spells it PD...)
        code = 'PD' + code[2:]
        if code in known:
            return known[code].lower()
    # a trailing HH / LL / H / L is the alarm or switch level
    level = ''
    for suffix in ('HH', 'LL', 'H', 'L'):
        if len(code) > 2 and code.endswith(suffix) and code[:-len(suffix)][-1:] in 'SA':
            level, code = ISA_LEVEL[suffix], code[:-len(suffix)]
            break
    if code in known:
        return f'{known[code].lower()} {level}'.strip()
    first = ISA_FIRST.get(code[0])
    if not first or len(code) < 2:
        return None
    rest = code[1:]
    modifier = ''
    if rest[0] in ISA_MODIFIER and len(rest) > 1:
        modifier, rest = ISA_MODIFIER[rest[0]] + ' ', rest[1:]
    functions = [ISA_FUNCTION.get(ch) for ch in rest]
    if not all(functions):
        return None
    return ' '.join(x for x in [modifier + first, *functions, level] if x)


def prompt_section():
    """The legend as a prompt section ('' when no legend has been built)."""
    ref = reference_text()
    if not ref:
        return ''
    return ('\n\n---\n\n## LEGEND REFERENCE\n'
            'Symbols, abbreviations, instrument codes and service-fluid codes, from a legend sheet. '
            "Use it to read symbols and tags by the standard instead of guessing. Where the drawing's own "
            'notes or legend say otherwise, the drawing wins.\n\n' + ref)


def describe(entity):
    """A decoded meaning for an instrument tag, for inventory listings, or ''."""
    if entity.get('category') != 'instrument' or not entity.get('tag'):
        return ''
    meaning = decode_instrument(entity['tag'])
    return f' = {meaning}' if meaning else ''
