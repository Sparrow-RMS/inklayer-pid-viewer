"""The HAZOP worksheet: deviation -> cause -> consequence -> safeguards / recommendations."""
import sqlite3

from fastapi.testclient import TestClient

from hazop_agent import worksheets
from hazop_agent.server import app

client = TestClient(app)
STUDY = 'pid-sample-8c299cdeb0b7'
API = '/agent/hazop'


def deviation(node='p0-node-1', **fields):
    body = {'nodeId': node, 'nodeLabel': 'N1 tank', 'parameter': 'Flow', 'guideword': 'NO/LESS', 'deviation': 'No/less flow to REA-V215', **fields}
    r = client.post(f'{API}/studies/{STUDY}/deviations', json=body)
    assert r.status_code == 201, r.text
    return r.json()


def worksheet(node=None):
    return client.get(f'{API}/studies/{STUDY}/worksheet', params={'nodeId': node} if node else {}).json()


def test_the_whole_hierarchy_is_built_and_read_back_in_order(jobs_dir):
    d = deviation()
    d = client.post(f"{API}/deviations/{d['id']}/causes", json={'text': 'XV1 stuck closed'}).json()
    d = client.post(f"{API}/deviations/{d['id']}/causes", json={'text': 'Line choked'}).json()
    cause = d['causes'][0]
    d = client.post(f"{API}/causes/{cause['id']}/consequences", json={'text': 'Batch stopped', 'severity': 1, 'likelihood': 3}).json()
    d = client.post(f"{API}/causes/{cause['id']}/consequences", json={'text': 'High pump discharge pressure', 'severity': 2, 'likelihood': 3}).json()
    cons = d['causes'][0]['consequences'][1]
    d = client.post(f"{API}/consequences/{cons['id']}/safeguards", json={'text': 'PAH 1258003'}).json()
    d = client.post(f"{API}/consequences/{cons['id']}/safeguards", json={'text': 'PG 1258007'}).json()
    d = client.post(f"{API}/consequences/{cons['id']}/recommendations", json={'text': 'Add PSV', 'owner': 'Process'}).json()

    (tree,) = worksheet('p0-node-1')
    assert [c['text'] for c in tree['causes']] == ['XV1 stuck closed', 'Line choked']
    first = tree['causes'][0]
    assert [q['text'] for q in first['consequences']] == ['Batch stopped', 'High pump discharge pressure']
    second = first['consequences'][1]
    assert (second['severity'], second['likelihood']) == (2, 3)
    assert [s['text'] for s in second['safeguards']] == ['PAH 1258003', 'PG 1258007']
    assert second['recommendations'][0]['owner'] == 'Process' and second['recommendations'][0]['status'] == 'open'
    assert first['consequences'][0]['safeguards'] == []


def test_items_are_edited_and_deleting_one_removes_everything_under_it(jobs_dir):
    d = deviation()
    d = client.post(f"{API}/deviations/{d['id']}/causes", json={'text': 'c', 'consequences': [
        {'text': 'q', 'severity': 3, 'likelihood': 2, 'safeguards': [{'text': 's'}], 'recommendations': [{'text': 'r'}]}]}).json()
    cons = d['causes'][0]['consequences'][0]
    rec = cons['recommendations'][0]

    edited = client.patch(f"{API}/consequences/{cons['id']}", json={'severity': None, 'text': 'q2'}).json()
    q = edited['causes'][0]['consequences'][0]
    assert q['severity'] is None and q['likelihood'] == 2 and q['text'] == 'q2'
    closed = client.patch(f"{API}/recommendations/{rec['id']}", json={'status': 'closed', 'owner': 'Ops'}).json()
    assert closed['causes'][0]['consequences'][0]['recommendations'][0]['status'] == 'closed'
    assert client.patch(f"{API}/causes/{d['causes'][0]['id']}", json={'severity': 3}).status_code == 422  # causes have no rating

    assert client.delete(f"{API}/causes/{d['causes'][0]['id']}").status_code == 204
    (tree,) = worksheet('p0-node-1')
    assert tree['causes'] == []
    with sqlite3.connect(worksheets.db_path()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM hazop_safeguards').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM hazop_recommendations').fetchone()[0] == 0


def test_bulk_adds_whole_deviations_and_the_summary_counts_them_per_node(jobs_dir):
    body = [{'nodeId': 'p0-node-1', 'parameter': 'Level', 'guideword': 'MORE', 'deviation': 'High level',
             'causes': [{'text': 'Inlet valve fails open', 'consequences': [{'safeguards': [{'text': 'LSHH-1'}, {'text': 'LAH-2'}]}]}]},
            {'nodeId': 'p0-node-2', 'parameter': 'Flow', 'guideword': 'REVERSE', 'deviation': 'Reverse flow'}]
    r = client.post(f'{API}/studies/{STUDY}/deviations/bulk', json=body)
    assert r.status_code == 201
    assert [s['text'] for s in r.json()[0]['causes'][0]['consequences'][0]['safeguards']] == ['LSHH-1', 'LAH-2']
    assert client.get(f'{API}/studies/{STUDY}/summary').json() == {'p0-node-1': 1, 'p0-node-2': 1}


def test_ratings_and_status_are_validated_and_unknown_items_are_404(jobs_dir):
    d = deviation()
    assert client.post(f"{API}/deviations/{d['id']}/causes", json={'consequences': [{'severity': 6}]}).status_code == 422
    assert client.post(f'{API}/studies/{STUDY}/deviations', json={'nodeId': ''}).status_code == 422
    assert client.patch(f'{API}/deviations/nope', json={'deviation': 'x'}).status_code == 404
    assert client.delete(f'{API}/safeguards/nope').status_code == 404
    assert client.post(f'{API}/causes/nope/consequences', json={}).status_code == 404


def test_the_study_package_routes_are_not_shadowed(jobs_dir):
    r = client.post(f'{API}/studies/{STUDY}/documents', params={'docType': 'pfd', 'fileName': 'p.pdf'}, content=b'%PDF')
    assert client.patch(f"{API}/documents/{r.json()['id']}", json={'note': 'n'}).json()['note'] == 'n'


def test_rows_in_the_old_flat_format_are_migrated_once(jobs_dir):
    path = worksheets.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE hazop_rows (id TEXT, study_id TEXT, node_id TEXT, node_label TEXT, guideword TEXT, parameter TEXT,
            cause TEXT, consequence TEXT, safeguards TEXT, severity INTEGER, likelihood INTEGER, recommendation TEXT, owner TEXT,
            status TEXT, created_at TEXT, updated_at TEXT)""")
        conn.execute("INSERT INTO hazop_rows VALUES ('r1', ?, 'p0-node-1', 'N1', 'MORE', 'ST-V303 inventory', 'continued receipt', "
                     "'overfill', 'LSHH-1\nnone shown\nLAH-2', 4, 2, 'add trip', 'Ops', 'in progress', '2026-01-01', '2026-01-01')", (STUDY,))
        conn.execute("INSERT INTO hazop_rows VALUES ('r2', ?, 'p0-node-1', 'N1', 'NO', 'flow into ST-V303', 'closed valve', "
                     "'', 'none shown', NULL, NULL, '', '', 'open', '2026-01-02', '2026-01-02')", (STUDY,))

    first, second = worksheet('p0-node-1')
    assert (first['parameter'], first['guideword'], first['deviation']) == ('Level', 'MORE', 'MORE ST-V303 inventory')
    q = first['causes'][0]['consequences'][0]
    assert q['text'] == 'overfill' and (q['severity'], q['likelihood']) == (4, 2)
    assert [s['text'] for s in q['safeguards']] == ['LSHH-1', 'LAH-2']  # "none shown" is not a safeguard
    assert q['recommendations'][0] == {**q['recommendations'][0], 'text': 'add trip', 'owner': 'Ops', 'status': 'in progress'}
    assert second['parameter'] == 'Flow' and second['causes'][0]['text'] == 'closed valve'
    assert second['causes'][0]['consequences'] == []  # nothing beyond the cause to carry over
    assert len(worksheet()) == 2  # migrated once, not again on the next read
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'hazop_rows_legacy'").fetchone()
