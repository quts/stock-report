"""Investment report rendering: sections, charts, limited-report labelling, page budget."""
from pathlib import Path
import unicodedata
from pypdf import PdfReader
from stock_report import engine
from stock_report.analysis import synthetic_proposal
from stock_report.models import Request
from stock_report.pipeline import finalize, load_prepared, prepare
from stock_report.policy import evaluate
from stock_report.render import render
from stock_report.storage import read_json, write_json
from engine_fixtures import dell
from test_investment_engine import proposal_for

HEADINGS = ('摘要', '成長、利潤率與現金轉換', '市場接下來要驗證什麼', '價格反映了什麼', '這份研究知道什麼、不知道什麼')


def render_fixture(tmp_path, data, proposal):
    decision = evaluate(data, proposal)
    investment = engine.run(data, proposal, decision)
    path = tmp_path / 'report.pdf'
    pages = render(data, decision, read_json(Path('config/ratings.json')), path,
                   company_research=proposal.company_research, investment=investment)
    text = unicodedata.normalize('NFKC', ''.join(p.extract_text() for p in PdfReader(str(path)).pages))
    return pages, text, investment


def test_complete_investment_report_has_all_sections_and_no_limited_banner(tmp_path):
    data = dell()
    pages, text, investment = render_fixture(tmp_path, data, proposal_for(data))
    assert investment['gates']['grade'] == 'complete'
    assert 4 <= pages <= 8
    for heading in HEADINGS:
        assert heading in text
    assert '受限報告' not in text
    assert 'Bull' in text and 'Base' in text and 'Bear' in text
    assert '假設性估值敏感度' in text and '稅率' in text and '自由現金流' in text and '存貨天數' in text
    assert '公司指引' in text and '不能代替市場共識' in text


def test_missing_price_is_published_as_a_clearly_limited_report(tmp_path):
    data = dell(price=None)
    pages, text, investment = render_fixture(tmp_path, data, proposal_for(data))
    assert investment['gates']['grade'] == 'limited'
    assert '受限報告' in text and '估值不可用' in text


def test_pipeline_writes_investment_inputs_and_audit_for_investment_profile(tmp_path):
    request = Request(mode='stock', ticker='NVDA', exchange='NASDAQ', research_as_of='2026-11-14T17:00:00+08:00',
                      report_profile='investment', max_pdf_pages=8)
    prepared = prepare(request, tmp_path)
    run = Path(prepared['research_input_path']).parent
    assert (run / 'investment_inputs.json').is_file()
    _, data, _ = load_prepared(run)
    proposal = synthetic_proposal(data)
    write_json(run / 'ai_proposal.json', proposal)
    result = finalize(run, run / 'ai_proposal.json', Path('config/ratings.json'))
    research = read_json(run / 'research.json')
    assert research['report_grade'] == result['report_grade'] == 'limited'   # synthetic proposal has no scenarios
    assert 'investment.json' in read_json(run / 'manifest.json')['hashes']
    qa = read_json(run / 'qa.json')
    assert {c['status'] for c in qa['quality_gates']} <= {'PASS', 'WARNING', 'FAIL', 'NOT_APPLICABLE'}
    assert qa['market_data']['diagnostics']['providers']
