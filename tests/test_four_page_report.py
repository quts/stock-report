"""Readable four-page research format: sections, footnote numbering, page budget, no gate jargon."""
from pathlib import Path
import re
import unicodedata
import pytest
from pypdf import PdfReader
from stock_report.analysis import synthetic_proposal
from stock_report.models import CitedParagraph, CompanyResearch
from stock_report.pipeline import finalize, load_prepared, prepare
from stock_report.policy import evaluate
from stock_report.render import projection, render
from stock_report.storage import read_json, write_json

SECTIONS = ('overview', 'key_takeaways', 'fundamentals', 'news', 'earnings_preview', 'monitoring')
HEADINGS = ('公司如何賺錢', '這次研究的重點', '如何變成現金', '市場接下來看什麼', '未來兩個研究期間', '來源索引')


def company_research(data):
    ids = [e.evidence_id for e in data.evidence if e.entity_id == data.request.ticker]
    return CompanyResearch(entity_id=data.request.ticker, **{
        field: [CitedParagraph(text=f'測試段落：{field} 的業務分析內容。', evidence_ids=ids[:2])] for field in SECTIONS})


def pdf_text(path):
    pdf = PdfReader(str(path))
    return pdf, unicodedata.normalize('NFKC', ''.join(p.extract_text() for p in pdf.pages))


def forward(data, evidence_id, kind, **changes):
    base = next(e for e in data.evidence if e.kind == 'financial' and e.entity_id == data.request.ticker)
    return base.model_copy(update=dict(evidence_id=evidence_id, statement_kind=kind, target_period='Q4 FY2027',
                                       financial_metric='revenue', **changes))


def rendered(data, proposal, mapping, tmp_path, research=None):
    research = [company_research(data)] if research is None else research
    path = tmp_path / 'report.pdf'
    pages = render(data, evaluate(data, proposal), mapping, path, company_research=research)
    return pages, path


def test_four_page_layout_has_all_reader_sections_within_budget(data, proposal, mapping, tmp_path):
    pages, path = rendered(data, proposal, mapping, tmp_path)
    pdf, text = pdf_text(path)
    assert 1 <= pages == len(pdf.pages) <= 4
    for heading in HEADINGS:
        assert heading in text
    assert '業務分析內容' in text


def test_body_uses_footnote_numbers_not_internal_ids_or_gate_jargon(data, proposal, mapping, tmp_path):
    _, path = rendered(data, proposal, mapping, tmp_path)
    _, text = pdf_text(path)
    assert not any(e.evidence_id in text for e in data.evidence)
    assert re.search(r'\[\d+\]', text)
    for jargon in ('Policy Gate', 'Gate', 'CORE_INSUFFICIENT', 'LATEST_FINANCIAL', 'rule_id'):
        assert jargon not in text


def test_limitations_are_stated_once_in_the_appendix(data, proposal, mapping, tmp_path):
    _, path = rendered(data, proposal, mapping, tmp_path)
    pdf, text = pdf_text(path)
    assert text.count('不作個人化交易指令') == 1
    assert '資料範圍' in unicodedata.normalize('NFKC', pdf.pages[-1].extract_text())


def test_insufficient_valuation_keeps_factual_business_analysis(data, proposal, mapping, tmp_path):
    for p in proposal.outlooks:
        p.outlook = 'insufficient'
    _, path = rendered(data, proposal, mapping, tmp_path)
    _, text = pdf_text(path)
    assert all(f'{field} 的業務分析內容' in text for field in SECTIONS)


def test_guidance_and_external_estimates_are_labelled_separately_and_scenarios_are_not_estimates(
        data, proposal, mapping, tmp_path):
    data.evidence += [forward(data, 'test-guidance', 'management_guidance'),
                      forward(data, 'test-consensus', 'consensus_estimate', institution='Named test provider'),
                      forward(data, 'test-scenario', 'scenario')]
    view = projection(data, evaluate(data, proposal), mapping, [company_research(data)])
    kinds = {e.evidence_id: e.statement_kind for e in view['companies'][0]['guidance']}
    assert kinds == {'test-guidance': 'management_guidance', 'test-consensus': 'consensus_estimate'}
    _, path = rendered(data, proposal, mapping, tmp_path)
    _, text = pdf_text(path)
    assert '公司指引' in text and '市場共識' in text and '具名分析師預測' not in text


def test_forward_statements_are_not_statement_rows(data, proposal, mapping):
    data.evidence.append(forward(data, 'test-guidance', 'management_guidance'))
    view = projection(data, evaluate(data, proposal), mapping, [company_research(data)])
    rows = view['companies'][0]['quarter_rows'] + view['companies'][0]['balance_rows']
    assert not any('test-guidance' in r['evidence_ids'] for r in rows)


def test_all_material_events_are_shown_in_the_two_page_profile(data, proposal, mapping):
    from stock_report.models import ReviewedEvent
    base = next(e for e in data.evidence if e.kind == 'event')
    extra = [base.model_copy(update={'evidence_id': f'test-major-{i}'}) for i in range(4)]
    data.evidence += extra
    data.event_review.events = [ReviewedEvent(event_id=f'test-major-{i}', title=f'Test event {i}',
        category='management_change', materiality='major', status='occurred', event_time=base.event_time,
        verification='official', evidence_ids=[e.evidence_id]) for i, e in enumerate(extra)]
    view = projection(data, evaluate(data, proposal), mapping)
    shown = {e.evidence_id for e in view['companies'][0]['display_events']}
    assert {e.evidence_id for e in extra} <= shown


def test_two_page_request_keeps_legacy_layout(data, proposal, mapping, tmp_path):
    data.request = data.request.model_copy(update={'max_pdf_pages': 2})
    pages, path = rendered(data, proposal, mapping, tmp_path)
    _, text = pdf_text(path)
    assert pages <= 2 and '公司如何賺錢' not in text


def test_pipeline_finalizes_four_page_research_and_records_it(tmp_path, request_model):
    prepared = prepare(request_model, tmp_path)
    run = Path(prepared['research_input_path']).parent
    _, data, _ = load_prepared(run)
    proposal = synthetic_proposal(data)
    proposal.company_research = [company_research(data)]
    write_json(run / 'ai_proposal.json', proposal)
    result = finalize(run, run / 'ai_proposal.json', Path('config/ratings.json'))
    assert 1 <= read_json(run / 'manifest.json')['pdf_pages'] <= 4
    research = read_json(run / 'research.json')
    assert research['company_research'][0]['entity_id'] == 'NVDA'
    assert 'statement_metrics.json' in read_json(run / 'manifest.json')['hashes']
    assert Path(result['report_path']).is_file()


def test_bad_citation_in_company_research_blocks_publication(tmp_path, request_model):
    prepared = prepare(request_model, tmp_path)
    run = Path(prepared['research_input_path']).parent
    _, data, _ = load_prepared(run)
    proposal = synthetic_proposal(data)
    bad = company_research(data)
    bad.news = [CitedParagraph(text='不存在的引用', evidence_ids=['does-not-exist'])]
    proposal.company_research = [bad]
    write_json(run / 'ai_proposal.json', proposal)
    with pytest.raises(ValueError, match='unlicensed'):
        finalize(run, run / 'ai_proposal.json', Path('config/ratings.json'))
    assert not (run / 'report.pdf').exists()


def test_incomplete_event_review_is_stated_in_the_appendix(data, proposal, mapping, tmp_path):
    data.event_review = None
    view = projection(data, evaluate(data, proposal), mapping, [company_research(data)])
    assert any('重大事件查核未完成' in item for item in view['limitations'])


def test_tracking_conditions_do_not_double_punctuate(data, proposal, mapping):
    for p in proposal.outlooks:
        p.invalidation_conditions = ['條件一。', '條件二。']
    view = projection(data, evaluate(data, proposal), mapping, [company_research(data)])
    assert all(c in ('條件一', '條件二') for o in view['companies'][0]['reader_outlooks'] for c in o['conditions'])
