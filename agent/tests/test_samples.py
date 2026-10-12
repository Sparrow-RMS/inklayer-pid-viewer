"""The reference library of accepted HAZOP worksheets (the model calls are replaced)."""
from hazop_agent.pipeline import samples

ROWS = [
    {'node': 'Node 2: Tank T-201 receipt', 'nodeType': 'storage', 'parameter': 'level', 'guideword': 'MORE', 'deviation': 'High level in T-201',
     'causes': [{'cause': 'LIC-201 malfunction resulting in inlet valve LV-201 failing open', 'consequences': [
         {'consequence': 'Potential overfilling of T-201 leading to product spill in dyke and fire hazard',
          'safeguards': ['LAH-201 high level alarm', 'LSHH-202 closes XV-201'], 'recommendation': ''}]}]},
    {'node': 'Node 5: Pump P-301A/B', 'nodeType': 'transfer', 'parameter': 'level', 'guideword': 'MORE', 'deviation': 'High level',
     'causes': [{'cause': 'Operator error', 'consequences': []}]},
    {'node': 'Node 2: Tank T-201 receipt', 'nodeType': 'storage', 'parameter': 'flow', 'guideword': 'NO', 'deviation': 'No flow to T-201',
     'causes': [{'cause': 'Inadvertent closure of manual valve on line 6"-P-201', 'consequences': [
         {'consequence': 'Loss of receipt; upstream unit shutdown', 'safeguards': [], 'recommendation': 'Provide LO on the inlet valve'}]}]},
]


def fake_model(monkeypatch, calls):
    async def call(*, system, schema, schema_name, content, model=None):
        calls.append(schema_name)
        if schema_name == 'hazop_sample_rows':
            return {'rows': ROWS}
        return {'styleGuide': 'Causes name the instrument or valve and its failure mode, e.g. "LIC-201 malfunction resulting in ...".'}
    monkeypatch.setattr(samples, 'structured_call', call)


def use_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(samples, 'SAMPLES_DIR', tmp_path)
    monkeypatch.setattr(samples, 'LIBRARY', tmp_path / 'library.json')


async def test_reports_are_read_once_and_a_style_guide_is_written(tmp_path, monkeypatch):
    use_folder(monkeypatch, tmp_path)
    (tmp_path / 'unit-200-hazop.txt').write_text('Node 2 ... worksheet ...')
    calls = []
    fake_model(monkeypatch, calls)
    library = await samples.build_library(on_progress=lambda m: None)
    assert calls == ['hazop_sample_rows', 'hazop_style']
    assert [f['fileName'] for f in library['files'].values()] == ['unit-200-hazop.txt'] and library['styleGuide']
    await samples.build_library(on_progress=lambda m: None)
    assert calls == ['hazop_sample_rows', 'hazop_style']  # cached: nothing read again
    (tmp_path / 'unit-200-hazop.txt').unlink()
    assert (await samples.build_library(on_progress=lambda m: None))['files'] == {}  # a removed report leaves the library


async def test_rows_for_the_same_deviation_and_kind_of_node_are_picked(tmp_path, monkeypatch):
    use_folder(monkeypatch, tmp_path)
    (tmp_path / 'r.txt').write_text('x')
    fake_model(monkeypatch, [])
    await samples.build_library(on_progress=lambda m: None)
    tank = {'categories': ['storage', 'drain']}
    rows = samples.examples_for(tank, [('level', 'MORE', 'level in TK-01'), ('flow', 'NO', 'flow in')])
    assert [r['node'] for r in rows] == ['Node 2: Tank T-201 receipt', 'Node 5: Pump P-301A/B', 'Node 2: Tank T-201 receipt']
    text = samples.prompt_section(tank, [('level', 'MORE', '')])
    assert 'Style guide:' in text and 'LIC-201 malfunction resulting in inlet valve LV-201 failing open' in text
    assert 'everything you write must be about THIS node' in text


def test_no_library_means_no_style_section(tmp_path, monkeypatch):
    use_folder(monkeypatch, tmp_path)
    assert samples.prompt_section({'categories': ['storage']}, [('level', 'MORE', '')]) == ''
