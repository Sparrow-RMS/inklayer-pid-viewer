"""The legend knowledge base and instrument tag decoding."""
import pytest

from hazop_agent.pipeline import nomenclature

pytestmark = pytest.mark.skipif(not nomenclature.legend()['sections'], reason='nomenclature.json not built')


def test_the_legend_tables_were_extracted():
    sections = nomenclature.legend()['sections']
    codes = {r['code']: r['meaning'] for r in sections['Abbreviation (General)']['entries']}
    assert codes['FC'] == 'Fail Closed' and codes['NC'] == 'Normally Closed' and codes['(E)'] == 'Existing'
    groups = {r.get('group') for r in sections['Instruments Identification']['entries']}
    assert {'Pressure', 'Temperature', 'Flow', 'Level'} <= groups
    assert [r['class'] for r in sections['Classification of Equipment']['entries']][:3] == ['A', 'B', 'C']


@pytest.mark.parametrize('tag, meaning', [
    ('LSHH-1257001', 'level switch high-high'),
    ('DPT-1257001', 'differential pressure transmitter'),  # DP... is differential pressure
    ('PSV-1257001', 'pressure safety valve'),
    ('XV1-1257020', 'on/off valve'),
    ('FIQ-12', 'flow indicator and totaliser'),  # the legend's own wording
    ('ZS-1257032', 'position switch'),
])
def test_instrument_tags_decode(tag, meaning):
    assert nomenclature.decode_instrument(tag) == meaning


def test_only_instruments_are_described():
    assert nomenclature.describe({'category': 'equipment', 'tag': 'ST-V303'}) == ''
    assert nomenclature.describe({'category': 'instrument', 'tag': 'LSHH-1'}) == ' = level switch high-high'

