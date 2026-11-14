from pathlib import Path
import sqlite3
import json
import subprocess
import sys
import pytest
from pypdf import PdfReader
from stock_report.models import Request
from stock_report.pipeline import prepare,load_prepared,finalize,replay
from stock_report.analysis import synthetic_proposal
from stock_report.storage import write_json,read_json
from stock_report.cli import main


def run_report(tmp_path, request):
    result=prepare(request,tmp_path)
    run=Path(result['research_input_path']).parent
    _,data,_=load_prepared(run)
    write_json(run/'ai_proposal.json',synthetic_proposal(data))
    return run,finalize(run,run/'ai_proposal.json',Path('config/ratings.json'))

@pytest.mark.parametrize('name',['stock-nvda','stock-twse','stock-tpex','event'])
def test_mock_e2e_pdf_and_audit(tmp_path,name):
    request=Request.model_validate(read_json(Path('examples')/f'{name}.json'))
    run,result=run_report(tmp_path,request)
    assert result['status']=='partial'
    assert Path(result['report_path']).exists()
    pdf=PdfReader(result['report_path'])
    assert len(pdf.pages)==2
    assert float(pdf.pages[0].mediabox.width)==pytest.approx(595.28,abs=1)
    text=''.join(p.extract_text() for p in pdf.pages)
    for required in ['SYNTHETIC DATA','投顧共識資料不足','3-6m','6-12m']:
        assert required in text
    assert '\ufffd' not in text
    assert read_json(run/'manifest.json')['pdf_pages']==2
    with sqlite3.connect(run/'evidence.sqlite') as db:
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0]>0
    assert set(read_json(run/'manifest.json')['shareable_files'])=={'report.pdf'}


def test_replay_values_gate_and_integrity(tmp_path,request_model):
    run,_=run_report(tmp_path,request_model)
    result=replay(run,tmp_path)
    new=Path(result['report_path']).parent
    assert new!=run
    assert read_json(new/'decision.json')==read_json(run/'decision.json')
    before,after=read_json(run/'research.json'),read_json(new/'research.json')
    before.pop('run_id');after.pop('run_id')
    assert before==after
    (run/'decision.json').write_text('{}')
    with pytest.raises(ValueError,match='integrity'):replay(run,tmp_path)


def test_changed_snapshot_or_proposal_rejected(tmp_path,request_model):
    result=prepare(request_model,tmp_path)
    run=Path(result['research_input_path']).parent
    _,data,_=load_prepared(run)
    proposal=synthetic_proposal(data)
    proposal.input_sha256='0'*64
    write_json(run/'wrong.json',proposal)
    with pytest.raises(ValueError,match='hash'):finalize(run,run/'wrong.json',Path('config/ratings.json'))
    data.evidence[0].claim='changed'
    write_json(run/'research_input.json',data)
    with pytest.raises(ValueError,match='snapshot'):load_prepared(run)
    assert not (run/'report.pdf').exists()


def test_cannot_overwrite_final_run(tmp_path,request_model):
    run,_=run_report(tmp_path,request_model)
    with pytest.raises(ValueError,match='already finalized'):
        finalize(run,run/'ai_proposal.json',Path('config/ratings.json'))


def test_restricted_contents_absent_from_pdf(tmp_path,request_model):
    result=prepare(request_model,tmp_path)
    run=Path(result['research_input_path']).parent
    _,data,_=load_prepared(run)
    restricted=next(e for e in data.evidence if e.source_record_id=='risk')
    restricted.claim='SECRET_RESTRICTED_CONTENT'
    restricted.shareable_in_pdf=False
    # Create a new trusted snapshot before signing the corresponding proposal.
    from stock_report.storage import digest
    write_json(run/'research_input.json',data)
    prepared=read_json(run/'prepared.json');prepared['input_sha256']=digest(data);write_json(run/'prepared.json',prepared)
    write_json(run/'ai_proposal.json',synthetic_proposal(data))
    result=finalize(run,run/'ai_proposal.json',Path('config/ratings.json'))
    text=''.join(p.extract_text() for p in PdfReader(result['report_path']).pages)
    assert 'SECRET_RESTRICTED_CONTENT' not in text
    assert all(p['outlook']=='insufficient' for p in read_json(run/'decision.json')['outlooks'])


def test_cli_contract_and_observable_errors(tmp_path,capsys):
    assert main(['prepare','--request','examples/stock-nvda.json','--provider','mock','--output',str(tmp_path)])==0
    prepared=json.loads(capsys.readouterr().out)
    run=Path(prepared['research_input_path']).parent
    assert main(['analyze','--run',str(run),'--synthetic'])==0
    assert json.loads(capsys.readouterr().out)['status']=='synthetic_proposal_ready'
    assert main(['finalize','--run',str(run),'--proposal',str(run/'ai_proposal.json')])==0
    assert Path(json.loads(capsys.readouterr().out)['report_path']).is_file()
    assert main(['prepare','--request','examples/stock-nvda.json','--provider','import'])==1
    assert json.loads(capsys.readouterr().out)['status']=='failed'


def test_layout_overflow_is_failure(tmp_path,request_model,monkeypatch):
    import weasyprint
    class Overflow:
        pages=[1,2,3]
    monkeypatch.setattr(weasyprint.HTML,'render',lambda *a,**k:Overflow())
    result=prepare(request_model,tmp_path);run=Path(result['research_input_path']).parent
    _,data,_=load_prepared(run)
    write_json(run/'ai_proposal.json',synthetic_proposal(data))
    with pytest.raises(ValueError,match='page budget'):finalize(run,run/'ai_proposal.json',Path('config/ratings.json'))
    assert not (run/'report.pdf').exists()
    assert not (run/'manifest.json').exists()


def test_renderer_recomputes_bad_quant_and_hides_unverified_relationship(data,proposal,mapping,request_model):
    from stock_report.render import projection
    from stock_report.policy import evaluate
    from stock_report.mock import fixture
    from stock_report.analysis import synthetic_proposal
    data.metrics[0].value=999
    view=projection(data,evaluate(data,proposal),mapping)
    assert all(m.value!=999 for m in view['companies'][0]['metrics'])
    request=request_model.model_copy(update={'mode':'event','ticker':None,'exchange':None,'event_query':'合成事件'})
    event=fixture(request)
    next(e for e in event.evidence if e.evidence_id=='NVDA-relationship').shareable_in_pdf=False
    view=projection(event,evaluate(event,synthetic_proposal(event)),mapping)
    company=next(c for c in view['companies'] if c['entity'].entity_id=='NVDA')
    assert company['relationship'] is None
    assert company['relationship_evidence_ids']==[]


def test_html_injection_is_escaped_and_resources_forbidden(tmp_path,data,proposal,mapping,monkeypatch):
    from stock_report.render import render
    from stock_report.policy import evaluate
    import weasyprint
    captured={}
    class Document:
        pages=[1,2]
        def write_pdf(self,path):Path(path).write_bytes(b'test')
    class HTML:
        def __init__(self,*,string,url_fetcher):captured.update(html=string,fetch=url_fetcher)
        def render(self):return Document()
    monkeypatch.setattr(weasyprint,'HTML',HTML)
    next(e for e in data.evidence if e.source_record_id=='risk').claim='<img src="file:///etc/passwd"> DO NOT EXECUTE'
    render(data,evaluate(data,proposal),mapping,tmp_path/'safe.pdf')
    assert '<img src=' not in captured['html']
    assert '&lt;img' in captured['html']
    for url in ['file:///etc/passwd','https://example.invalid/private']:
        with pytest.raises(ValueError):captured['fetch'](url)


def test_cli_invalid_arguments_are_json(capsys):
    assert main(['prepare','--provider','unexpected'])==1
    error=json.loads(capsys.readouterr().out)
    assert error['status']=='failed'
    assert error['error_type']=='ValueError'
