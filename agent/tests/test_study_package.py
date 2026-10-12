"""The study package: supporting documents per drawing and the readiness checklist."""
import json

from fastapi.testclient import TestClient

from hazop_agent.pipeline import jobs
from hazop_agent.server import app

client = TestClient(app)
STUDY = 'pid-sample-8c299cdeb0b7'


def package(study=STUDY):
    return client.get(f'/agent/hazop/studies/{study}/package').json()


def upload(doc_type, name='doc.pdf', body=b'%PDF-1.4 sample', study=STUDY):
    return client.post(f'/agent/hazop/studies/{study}/documents', params={'docType': doc_type, 'fileName': name}, content=body)


def status(pkg):
    return {i['id']: i['status'] for i in pkg['checklist']['items']}


def test_an_empty_study_lists_all_seven_documents_as_missing(jobs_dir):
    pkg = package()
    assert [t['id'] for t in pkg['documentTypes']] == ['pid', 'pfd', 'layout', 'interlocks', 'process_description', 'parameters', 'datasheets']
    assert set(status(pkg).values()) == {'missing'} and pkg['checklist']['ready'] is False


def test_documents_are_attached_listed_downloaded_retyped_and_removed(jobs_dir):
    doc = upload('pfd', 'PFD rev 2.pdf').json()
    upload('datasheets', 'pumps.xlsx', b'PK\x03\x04 sheet')
    upload('datasheets', 'psv.xlsx', b'PK\x03\x04 sheet 2')
    pkg = package()
    assert status(pkg)['pfd'] == 'provided' and status(pkg)['datasheets'] == 'provided'
    assert len(next(i for i in pkg['checklist']['items'] if i['id'] == 'datasheets')['documentIds']) == 2

    got = client.get(f"/agent/hazop/documents/{doc['id']}/file")
    assert got.status_code == 200 and got.content == b'%PDF-1.4 sample'
    assert 'PFD%20rev%202.pdf' in got.headers['content-disposition']  # RFC 5987 encoded

    moved = client.patch(f"/agent/hazop/documents/{doc['id']}", json={'docType': 'layout', 'note': 'actually the layout'}).json()
    assert moved['docType'] == 'layout' and moved['note'] == 'actually the layout'
    assert status(package())['pfd'] == 'missing'

    assert client.delete(f"/agent/hazop/documents/{doc['id']}").status_code == 204
    assert client.get(f"/agent/hazop/documents/{doc['id']}/file").status_code == 404
    assert not (jobs_dir / 'studies' / STUDY / 'documents' / doc['id']).exists()


def test_the_checklist_credits_what_the_drawing_already_shows(jobs_dir):
    job = jobs_dir / STUDY
    job.mkdir()
    (job / 'source.pdf').write_bytes(b'%PDF-1.4')
    jobs.write_artifact(STUDY, 'step1b-sheet', {'pages': [{'interlockTable': {'tablePresent': True, 'interlocks': [{}, {}, {}]}}]})
    jobs.write_artifact(STUDY, 'step1', {'pages': [{'entities': [
        {'category': 'equipment_list_row', 'tag': 'T-1', 'attributes': []},
        {'category': 'instrument', 'tag': 'LSHH-101', 'attributes': []},
    ]}]})
    items = {i['id']: i for i in package()['checklist']['items']}
    assert items['pid']['status'] == 'provided'  # the drawing itself
    assert items['interlocks']['status'] == 'partial' and '3 C&E rows' in items['interlocks']['fromDrawing']
    assert items['datasheets']['status'] == 'partial' and items['parameters']['status'] == 'partial'
    assert items['pfd']['status'] == 'missing' and items['pfd']['fromDrawing'] is None


def test_all_seven_provided_is_ready(jobs_dir):
    for t in ('pid', 'pfd', 'layout', 'interlocks', 'process_description', 'parameters', 'datasheets'):
        upload(t)
    checklist = package()['checklist']
    assert checklist['ready'] is True and checklist['missing'] == [] and checklist['provided'] == 7


def test_bad_uploads_are_refused(jobs_dir):
    assert upload('notes').status_code == 422
    assert upload('pfd', 'run.exe', b'MZ').status_code == 415
    assert upload('pfd', 'empty.pdf', b'').status_code == 400
    assert client.patch('/agent/hazop/documents/nope', json={'note': 'x'}).status_code == 404
    doc = upload('pfd').json()
    assert client.patch(f"/agent/hazop/documents/{doc['id']}", json={'docType': 'notes'}).status_code == 422


def test_a_path_in_the_file_name_cannot_escape_the_study_folder(jobs_dir):
    doc = upload('pfd', '../../evil.pdf').json()
    assert doc['fileName'] == 'evil.pdf'
    assert (jobs_dir / 'studies' / STUDY / 'documents' / doc['id'] / 'evil.pdf').exists()
