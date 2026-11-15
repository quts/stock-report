"""Personal-use-only reports: internal-use permission is the gate, redistribution rights are not.

Private use is never treated as a permission in itself, so unknown, restricted and internally
unlicensed records stay out of the PDF, the metrics and the proposals.
"""
from pathlib import Path
import re
import unicodedata
import pytest
from pydantic import ValidationError
from pypdf import PdfReader
from stock_report.analysis import synthetic_proposal
from stock_report.collect import build_bundle, collect
from stock_report.licensing import LicenseRegistry, load_registry
from stock_report.mock import fixture
from stock_report.models import HorizonProposal, Proposal, Request
from stock_report.pipeline import finalize, load_prepared, prepare
from stock_report.policy import evaluate
from stock_report.quant import calculate
from stock_report.render import projection, render
from stock_report.storage import digest, read_json, write_json
from test_collect import NOW, SEC, FakeClient, nvda_request, payloads

FIXTURES = Path('tests/fixtures')
LABEL_ZH, LABEL_EN = '僅供個人使用', 'Personal use only / Not for distribution'


def pages_text(path):
    return [unicodedata.normalize('NFKC', page.extract_text()) for page in PdfReader(path).pages]


def internal_only_registry(pdf_share=False):
    payload = read_json(FIXTURES / 'licenses-attested.json')
    for source in payload['sources']:
        if source['declared_status'] == 'confirmed':
            source['scope']['pdf_share_allowed'] = pdf_share
    return LicenseRegistry.model_validate(payload)


def sec_bundle(registry):
    collection = collect(SEC, 'NVDA', registry=registry, allow_network=True,
                         client=FakeClient(payloads('sec_nvda_concepts.json')), now=NOW)
    data, report = build_bundle(nvda_request(), [collection], registry=registry)
    assert report['status'] == 'ready'
    return data


def bullish(data):
    return Proposal(input_sha256=digest(data), model='manual-test-proposal', outlooks=[
        HorizonProposal(entity_id='NVDA', horizon=horizon, outlook='bullish',
                        supporting_evidence_ids=['sec-NVDA-revenue_current', 'sec-NVDA-net_income'],
                        interpretation='測試用解讀，僅驗證個人報告流程。',
                        invalidation_conditions=['年度營收或淨利轉為衰退時重新研究'])
        for horizon in ('3-6m', '6-12m')])


def test_fixture_registry_grants_internal_use_without_any_redistribution_right():
    registry = load_registry(FIXTURES / 'licenses-attested.json')
    snapshots = [registry.snapshot(s.source_id) for s in registry.sources]
    assert any(s['internal_use_allowed'] for s in snapshots)
    assert not any(s['pdf_share_allowed'] for s in snapshots)


def test_internally_allowed_non_shareable_evidence_reaches_the_personal_pdf(tmp_path):
    data = sec_bundle(internal_only_registry())
    assert data.dataset_kind == 'real'
    assert all(e.internal_use_allowed and e.license_status == 'confirmed' for e in data.evidence)
    assert not any(e.shareable_in_pdf for e in data.evidence)
    request = nvda_request()
    prepared = prepare(request, tmp_path, data.model_dump(mode='json'))
    assert prepared['coverage']['NVDA']['financial'] == 'available'
    run = Path(prepared['research_input_path']).parent
    _, stored, _ = load_prepared(run)
    write_json(run / 'ai_proposal.json', bullish(stored))
    result = finalize(run, run / 'ai_proposal.json', Path('config/ratings.json'))
    decision = read_json(run / 'decision.json')
    assert result['gate_status'] == 'REJECTED'
    assert result['research_complete'] is False
    assert [p['outlook'] for p in decision['outlooks']] == ['insufficient', 'insufficient']
    assert 'LICENSE' not in {f['rule_id'] for f in decision['findings']}
    # Supporting metrics and proposal citations built on non-shareable evidence are rendered.
    pages = pages_text(result['report_path'])
    text = ''.join(pages)
    assert len(pages) == 2 and 'Real Data' in text and 'SYNTHETIC' not in text
    assert '23.45' in text and '19.44' in text and 'sec-NVDA-revenue_current' in text
    assert '資料不足' in text and '偏多' not in text
    assert '財務快照' in text and '事件查核未完成' in text
    assert all(LABEL_ZH in page and LABEL_EN in page for page in pages)
    manifest = read_json(run / 'manifest.json')
    assert manifest['distribution'] == 'personal_use_only' and manifest['shareable_files'] == []
    assert read_json(run / 'research.json')['distribution'] == 'personal_use_only'
    # The legacy flag is carried through untouched: nothing in the pipeline sets it to true.
    assert not any(e['shareable_in_pdf'] for e in read_json(run / 'evidence.json'))


def test_legacy_share_flag_never_changes_the_decision_or_the_projection(mapping):
    private, legacy = sec_bundle(internal_only_registry()), sec_bundle(internal_only_registry(pdf_share=True))
    assert all(e.shareable_in_pdf for e in legacy.evidence) and not any(e.shareable_in_pdf for e in private.evidence)
    first, second = evaluate(private, bullish(private)), evaluate(legacy, bullish(legacy))
    assert first == second and calculate(private.evidence, NOW) == calculate(legacy.evidence, NOW)
    views = [projection(d, r, mapping) for d, r in ((private, first), (legacy, second))]
    assert [[m.name for m in v['companies'][0]['metrics']] for v in views][0] == \
        [m.name for m in views[1]['companies'][0]['metrics']]
    assert len(views[0]['sources']) == len(views[1]['sources']) == len(private.evidence)


@pytest.mark.parametrize('change', [{'license_status': 'unknown'}, {'license_status': 'restricted'},
                                    {'internal_use_allowed': False},
                                    # A redistribution flag can never stand in for internal-use permission.
                                    {'license_status': 'unknown', 'shareable_in_pdf': True},
                                    {'internal_use_allowed': False, 'shareable_in_pdf': True}])
def test_unknown_restricted_or_internally_unlicensed_evidence_stays_blocked(data, proposal, mapping, tmp_path, change):
    target = next(e for e in data.evidence if e.source_record_id == 'revenue_current')
    target.claim = 'BLOCKED_SOURCE_CONTENT'
    for name, value in change.items():
        setattr(target, name, value)
    decision = evaluate(data, proposal)
    assert all(p.outlook == 'insufficient' for p in decision.outlooks)
    assert any(f.rule_id == 'LICENSE' and target.evidence_id in f.evidence_ids for f in decision.findings)
    view = projection(data, decision, mapping)
    assert target.evidence_id not in {e.evidence_id for e in view['sources']}
    # Metrics derived from the blocked record are withheld as well.
    assert not any(target.evidence_id in m.evidence_ids for m in view['companies'][0]['metrics'])
    render(data, decision, mapping, tmp_path / 'blocked.pdf')
    text = ''.join(pages_text(tmp_path / 'blocked.pdf'))
    assert 'BLOCKED_SOURCE_CONTENT' not in text and '營收成長率' not in text and '偏多' not in text


@pytest.mark.parametrize('change', [{'license_status': 'unknown'}, {'internal_use_allowed': False}])
def test_no_report_without_an_internally_licensed_identity(data, proposal, mapping, tmp_path, change):
    identity = next(e for e in data.evidence if e.kind == 'identity')
    for name, value in change.items():
        setattr(identity, name, value)
    with pytest.raises(ValueError, match='internally licensed company identity'):
        render(data, evaluate(data, proposal), mapping, tmp_path / 'none.pdf')
    assert not (tmp_path / 'none.pdf').exists()


@pytest.mark.parametrize('change,message', [
    ({'internal_use_allowed': False}, 'without confirmed internal-use permission'),
    ({'license_status': 'unknown'}, 'unknown or restricted'),
    ({'license_status': 'restricted'}, 'unknown or restricted')])
def test_import_refuses_evidence_without_confirmed_internal_use(tmp_path, change, message):
    payload = sec_bundle(internal_only_registry()).model_dump(mode='json')
    payload['evidence'][1].update(change)
    with pytest.raises(ValueError, match=message):
        prepare(nvda_request(), tmp_path, payload)
    assert not list(tmp_path.iterdir())


def test_closed_registry_blocks_a_personal_report():
    registry = load_registry('tests/fixtures/licenses-closed.json')
    assert all(s.declared_status == 'unknown' for s in registry.sources)
    assert not any(registry.snapshot(s.source_id)['internal_use_allowed'] for s in registry.sources)
    collection = collect(SEC, 'NVDA', registry=registry, allow_network=True,
                         client=FakeClient(payloads('sec_nvda_concepts.json')), now=NOW)
    data, report = build_bundle(nvda_request(), [collection], registry=registry)
    assert data is None and report['status'] == 'blocked'
    assert {row['reason'] for row in report['exclusions']} == {'license_not_confirmed_for_internal_use'}


def test_requests_cannot_ask_for_a_sharing_profile():
    base = dict(mode='stock', ticker='NVDA', exchange='NASDAQ', research_as_of='2026-11-14T17:00:00+08:00')
    assert Request(**base).sharing_profile == 'private'
    assert Request(**base, sharing_profile='private').sharing_profile == 'private'
    with pytest.raises(ValidationError):
        Request(**base, sharing_profile='friends')
    for example in Path('examples').glob('*.json'):
        assert Request.model_validate(read_json(example)).sharing_profile == 'private'


@pytest.mark.parametrize('name', ['stock-nvda', 'stock-twse', 'stock-tpex', 'event'])
def test_demo_flow_stays_readable_labelled_and_within_two_pages(tmp_path, name):
    request = Request.model_validate(read_json(Path('examples') / f'{name}.json'))
    prepared = prepare(request, tmp_path)
    assert prepared['warnings'] == ['DEMO / SYNTHETIC DATA']
    run = Path(prepared['research_input_path']).parent
    _, data, _ = load_prepared(run)
    # Synthetic records carry no redistribution flag either; the demo exercises the private path.
    assert not any(e.shareable_in_pdf for e in data.evidence)
    write_json(run / 'ai_proposal.json', synthetic_proposal(data))
    result = finalize(run, run / 'ai_proposal.json', Path('config/ratings.json'))
    assert result['warnings'][0] == 'DEMO / SYNTHETIC DATA' and result['distribution'] == 'personal_use_only'
    pages = pages_text(result['report_path'])
    text = ''.join(pages)
    assert len(pages) == 2 and read_json(run / 'manifest.json')['pdf_pages'] == 2
    assert all(LABEL_ZH in page and LABEL_EN in page for page in pages)
    assert 'DEMO / SYNTHETIC DATA' in pages[0] and '不得轉寄' in pages[0]
    assert '�' not in text and '分享' not in text
    for required in ('偏多', '中性', '3-6m', '6-12m', '投顧共識資料不足', '品質與風險揭露'):
        assert required in text
    if request.mode == 'stock':
        assert '最近可得收盤' in text and '20.00' in text and '營收成長率' in text
    else:
        assert '事件狀態與時間軸' in text and 'Hypothetical' in text


def test_template_keeps_readable_type_sizes_and_the_private_label():
    template = Path('src/stock_report/templates/report.html').read_text()
    sizes = [float(value) for value in re.findall(r'font-size:\s*([0-9.]+)pt', template)]
    assert sizes and min(sizes) >= 8
    assert LABEL_EN in template and LABEL_ZH in template and 'DEMO / SYNTHETIC DATA' in template
    assert '分享' not in template
