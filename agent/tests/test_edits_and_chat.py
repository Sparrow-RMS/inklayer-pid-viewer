"""Reviewer edits to the node breakdown, and the chat assistant that makes them."""
import json
import sqlite3

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from hazop_agent import chat
from hazop_agent.pipeline import edits, jobs
from hazop_agent.server import app
from hazop_agent.worksheets import db_path

client = TestClient(app)
JOB = 'pid-sample-8c299cdeb0b7'


def _node(i, entity_ids, typical_of=None, revised=False):
    hazop = {'localId': f'N{i}', 'name': f'Tank {i}', 'boundaryFrom': [], 'boundaryTo': [], 'equipmentTags': [f'TK-0{i}'],
             'designIntent': f'store in TK-0{i}', 'designConditions': {'pressure': 'ATM', 'temperature': 'AMB', 'material': 'CS', 'source': ''},
             'controlLoops': [], 'interlockRefs': [f'I-0{i}'], 'reliefDevices': [], 'parameters': [], 'vacuumProtection': None,
             'keyDeviations': [], 'operatingModes': [], 'utilitiesAsSafeguards': [], 'typicalOf': typical_of,
             'typicalOfName': 'Tank 1' if typical_of else None, 'differencesFromTypical': [], 'revisedScope': revised,
             'justification': [{'rule': 'O1', 'reason': 'holding entity'}], 'unverified': []}
    return {'id': f'p0-node-{i}', 'page': 0, 'label': f'N{i} Tank {i}', 'description': '', 'categories': ['storage'],
            'processIds': [f'pr{i}'], 'memberCount': 1, 'entityIds': entity_ids, 'primaryEquipmentIds': entity_ids[:1],
            'bbox': {'x': 0.1 * i, 'y': 0.1, 'width': 0.1, 'height': 0.1},
            'routes': [{'entityId': f's{i}', 'points': [{'x': 0.1 * i, 'y': 0.1}, {'x': 0.1 * i + 0.05, 'y': 0.1}], 'flow': True}],
            'hazop': hazop}


def make_job():
    jobs.ensure_job(JOB, fileName='pid-sample.pdf')
    jobs.write_artifact(JOB, 'step3', {'pages': [{'pageIndex': 0, 'nodes': [
        _node(1, ['a', 'b']), _node(2, ['c', 'd'], typical_of='p0-node-1'), _node(3, ['e'], typical_of='p0-node-2')]}]})


def worksheet_row(node_id):
    r = client.post(f'/agent/hazop/studies/{JOB}/deviations', json={'nodeId': node_id, 'nodeLabel': 'x', 'parameter': 'Flow',
                                                                       'guideword': 'NO', 'deviation': f'no flow {node_id}'})
    return r.json()['id']


def nodes_of(document):
    return [(n['id'], n['label']) for p in document['pages'] for n in p['nodes']]


def test_a_merge_combines_the_nodes_renumbers_them_and_moves_worksheet_rows(jobs_dir):
    make_job()
    row = worksheet_row('p0-node-2')
    edit, doc = edits.merge_nodes(JOB, ['p0-node-1', 'p0-node-2'], 'HSD tanks receipt', 'same process')
    assert nodes_of(doc) == [('p0-node-1', 'N1 HSD tanks receipt'), ('p0-node-3', 'N3 Tank 3')]  # numbers stay
    merged = doc['pages'][0]['nodes'][0]
    assert merged['entityIds'] == ['a', 'b', 'c', 'd'] and len(merged['routes']) == 2
    assert merged['hazop']['interlockRefs'] == ['I-01', 'I-02'] and merged['hazop']['typicalOf'] is None
    assert merged['hazop']['justification'][-1]['reason'].startswith('Merged by the reviewer: N1 Tank 1, N2 Tank 2 (same process)')
    # a node typical of a merged-away node is typical of the merged one now
    assert doc['pages'][0]['nodes'][1]['hazop']['typicalOf'] == 'p0-node-1'
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute('SELECT node_id, node_label FROM hazop_deviations WHERE id = ?', (row,)).fetchone() == (
            'p0-node-1', 'N1 HSD tanks receipt')
    # kept with the job, on top of the untouched pipeline result
    assert edits.current_document(JOB) == doc and len(jobs.read_artifact(JOB, 'step3')['pages'][0]['nodes']) == 3
    # undo brings back both nodes and the worksheet row
    edits.undo_last(JOB)
    assert len(nodes_of(edits.current_document(JOB))) == 3
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute('SELECT node_id FROM hazop_deviations WHERE id = ?', (row,)).fetchone() == ('p0-node-2',)


def test_an_edit_whose_nodes_changed_in_a_rerun_is_not_applied(jobs_dir):
    make_job()
    edits.merge_nodes(JOB, ['p0-node-1', 'p0-node-2'])
    doc = jobs.read_artifact(JOB, 'step3')
    doc['pages'][0]['nodes'][1]['entityIds'] = ['c', 'z']  # step 3 re-run: node 2 is something else now
    jobs.write_artifact(JOB, 'step3', doc)
    applied, stale = edits.apply_edits(doc, edits.load_edits(JOB, 'step3'))
    assert len(stale) == 1 and len(nodes_of(applied)) == 3


def test_rename_and_bad_merges(jobs_dir):
    make_job()
    _, doc = edits.rename_node(JOB, 'p0-node-3', 'MS tank')
    assert ('p0-node-3', 'N3 MS tank') in nodes_of(doc)
    for ids in (['p0-node-1'], ['p0-node-1', 'p0-node-9']):
        try:
            edits.merge_nodes(JOB, ids)
            raise AssertionError('should not merge')
        except ValueError:
            pass


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def scripted(monkeypatch, *replies):
    chat._agents.clear()
    monkeypatch.setattr(chat, 'ChatOpenAI', lambda **kwargs: ScriptedModel(messages=iter(replies)))


def stream(message):
    r = client.post('/agent/chat', json={'threadId': JOB, 'message': message})
    assert r.status_code == 200, r.text
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def test_the_chat_merges_on_the_drawings_thread_and_keeps_the_conversation(jobs_dir, monkeypatch):
    make_job()
    scripted(monkeypatch,
             AIMessage('', tool_calls=[{'name': 'merge_nodes', 'id': 'c1',
                                        'args': {'node_ids': ['p0-node-1', 'p0-node-2'], 'new_name': '', 'reason': 'same process'}}]),
             AIMessage('Merged N1 and N2 into one node; the drawing now has 2 nodes.'))
    events = stream('node 1 and node 2 are the same process, make them one node')
    assert [e['type'] for e in events] == ['tool', 'nodes', 'message']
    assert len(events[1]['document']['pages'][0]['nodes']) == 2
    assert events[2]['text'].startswith('Merged N1 and N2')
    # the thread is the drawing: the conversation comes back for it, and only for it
    history = client.get(f'/agent/chat/{JOB}').json()['messages']
    assert [m['role'] for m in history] == ['user', 'assistant']
    assert client.get('/agent/chat/another-drawing-000000000000').json()['messages'] == []
    # the system prompt carried the drawing's current nodes
    assert 'N1 Tank 1 + Tank 2' in chat.nodes_summary(JOB)


def test_the_chat_needs_a_thread_and_a_message(jobs_dir):
    assert client.post('/agent/chat', json={'message': 'hi'}).status_code == 422
