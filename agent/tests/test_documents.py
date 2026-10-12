"""Study-package documents in the pipeline: reading them, and recomputing when they change."""
import io
import zipfile

from fastapi.testclient import TestClient

from hazop_agent.pipeline import documents
from hazop_agent.server import app

from .conftest import FIXTURE_PDF
from .test_graph import fake_model, run  # noqa: F401 — fixture

client = TestClient(app)
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def docx(paragraphs, table=()):
    rows = ''.join('<w:tr>' + ''.join(f'<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>' for c in row) + '</w:tr>' for row in table)
    body = ''.join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs) + (f'<w:tbl>{rows}</w:tbl>' if table else '')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('word/document.xml', f'<w:document {W}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def xlsx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('xl/sharedStrings.xml', f'<sst {S}><si><t>Tag</t></si><si><t>Set point</t></si><si><t>LAHH-101</t></si></sst>')
        z.writestr('xl/worksheets/sheet1.xml', f'<worksheet {S}><sheetData>'
                   '<row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
                   '<row><c t="s"><v>2</v></c><c><v>92</v></c></row></sheetData></worksheet>')
    return buf.getvalue()


def test_word_excel_and_text_files_are_read_with_their_tables(tmp_path):
    (tmp_path / 'd.docx').write_bytes(docx(['Receipt mode: MOV open.'], [('Tank', 'Product'), ('TK-01', 'HSD')]))
    (tmp_path / 's.xlsx').write_bytes(xlsx())
    (tmp_path / 'c.csv').write_text('tag,value\nPT-1,3.5\n')
    (tmp_path / 'p.dwg').write_bytes(b'AC1032')
    assert documents.extract_text(tmp_path / 'd.docx') == 'Receipt mode: MOV open.\nTank | Product\nTK-01 | HSD'
    assert documents.extract_text(tmp_path / 's.xlsx') == '[sheet1]\nTag | Set point\nLAHH-101 | 92'
    assert 'PT-1,3.5' in documents.extract_text(tmp_path / 'c.csv')
    assert documents.extract_text(tmp_path / 'p.dwg') is None


def attach(study, doc_type, name, body):
    r = client.post(f'/agent/hazop/studies/{study}/documents', params={'docType': doc_type, 'fileName': name}, content=body)
    assert r.status_code == 201, r.text


async def test_attached_documents_reach_the_model_and_changing_them_recomputes(jobs_dir, fake_model):  # noqa: F811
    first = await run({'pdf_path': str(FIXTURE_PDF)})
    job = first['job_id']
    assert first['documents'] == []
    fake_model.calls.clear(); fake_model.prompts.clear()

    # Unchanged documents: everything is reused.
    again = await run({'job_id': job})
    assert again['documents_changed'] is False and again['step_status']['step2'] == 'cached'

    attach(job, 'process_description', 'Description.docx', docx(['Receipt and dispatch share one line; the MOV and inlet XV are open in receipt mode.']))
    attach(job, 'interlocks', 'interlock.docx', docx(['I-02 is fed by the level switch alarm and acts on the inlet XV and the MOV.']))
    attach(job, 'pid', 'same-drawing.pdf', FIXTURE_PDF.read_bytes())  # the drawing itself is never re-read as a document
    fake_model.prompts.clear()

    state = await run({'job_id': job})
    assert state['documents_changed'] is True
    assert {state['step_status'][s] for s in ('step2', 'sheet', 'step3')} == {'computed'}
    assert state['step_status']['step1'] == 'cached'
    prompts = dict(fake_model.prompts)
    assert 'Receipt and dispatch share one line' in prompts['pid_page_processes']
    assert 'Receipt and dispatch share one line' in prompts['pid_hazop_nodes']
    assert 'I-02 is fed by the level switch alarm' in prompts['pid_interlock_table']
    assert 'Receipt and dispatch' not in prompts['pid_interlock_table']  # the sheet step gets the interlock list only
    assert [d['type'] for d in state['documents']] == ['process_description', 'interlocks']

    # And once used, the same documents don't recompute again.
    fake_model.calls.clear()
    settled = await run({'job_id': job})
    assert settled['documents_changed'] is False and 'pid_hazop_nodes' not in fake_model.calls
