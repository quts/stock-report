"""Source activation package: review-ready, grants nothing, and bounds what a human may open."""
import copy
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from pypdf import PdfReader
from stock_report.activation import ActivationPackage, check
from stock_report.cli import main
from stock_report.collect import build_bundle, collect
from stock_report.licensing import LicenseRegistry, load_registry
from stock_report.models import HorizonProposal, Proposal
from stock_report.pipeline import finalize, load_prepared, prepare
from stock_report.storage import digest, read_json, write_json
from test_collect import NOW, TWSE, FakeClient, payloads, tsmc_request

PACKAGE = Path('docs/phase-0/2330-source-activation.json')
REGISTRY = Path('config/source-licenses.json')
INCOME = 'twse-openapi-income-general'


def instruments():
    return read_json('config/tw-instruments.json')


def candidate_payload():
    """A test-only stand-in for the edit a human would make. It is never written to config/."""
    payload = read_json(REGISTRY)
    item = next(i for i in read_json(PACKAGE)['items'] if i['source_id'] == TWSE)
    payload['registry_version'] = '0.5.0'
    source = next(s for s in payload['sources'] if s['source_id'] == TWSE)
    source.update(declared_status='confirmed', scope=item['proposed_scope'],
                  attribution=item['proposed_attribution'], attested_by='Unit Test Person',
                  attested_at='2026-11-16T10:00:00+08:00')
    for url in (item['dataset_page'], item['license_url']):
        source['terms_evidence'].append({'terms_url': url, 'checked_at': '2026-11-16T09:00:00+08:00',
                                         'checked_by': 'human', 'checker': 'Unit Test Person',
                                         'excerpt': 'UNIT TEST TEXT - not a quotation of any real terms page.',
                                         'excerpt_covers': 'unit test only'})
    return payload


def twse(payload, source_id=TWSE):
    return next(s for s in payload['sources'] if s['source_id'] == source_id)


def run(tmp_path, payload):
    path = tmp_path / 'candidate.json'
    write_json(path, payload)
    return check(PACKAGE, load_registry(REGISTRY), instruments(), path)


def test_package_is_pending_and_the_registry_stays_closed():
    before = REGISTRY.read_bytes()
    result = check(PACKAGE, load_registry(REGISTRY), instruments())
    assert result['status'] == 'awaiting_human_attestation' and result['registry_modified'] is False
    assert result['package_drift'] == [] and result['required_human_actions']
    assert {i['source_id']: i['registry_status'] for i in result['items']} == {TWSE: 'unknown', INCOME: 'unknown'}
    assert REGISTRY.read_bytes() == before
    registry = load_registry(REGISTRY)
    assert not any(registry.snapshot(s.source_id)['internal_use_allowed'] for s in registry.sources)
    package = read_json(PACKAGE)
    assert package['human_attestation'] is None and package['prepared_by_kind'] == 'assistant'


def test_package_proposes_normalized_internal_use_only():
    package = ActivationPackage.model_validate(read_json(PACKAGE))
    ready = [i for i in package.items if i.recommendation == 'ready_for_human_review']
    assert [i.source_id for i in ready] == [TWSE]
    assert ready[0].proposed_scope.model_dump() == {'internal_use_allowed': True, 'pdf_share_allowed': False,
                                                   'store_response_hash': False, 'store_raw_response': False}
    assert not any(next(i for i in package.items if i.source_id == INCOME).proposed_scope.model_dump().values())


@pytest.mark.parametrize('field', ['store_response_hash', 'store_raw_response', 'pdf_share_allowed'])
def test_package_cannot_propose_raw_hash_or_redistribution(field):
    payload = read_json(PACKAGE)
    payload['items'][0]['proposed_scope'][field] = True
    with pytest.raises(ValidationError, match='stay closed'):
        ActivationPackage.model_validate(payload)


def test_package_cannot_carry_an_attestation_or_open_an_unrecommended_item():
    payload = read_json(PACKAGE)
    payload['human_attestation'] = {'attested_by': 'someone'}
    with pytest.raises(ValidationError):
        ActivationPackage.model_validate(payload)
    payload = read_json(PACKAGE)
    payload['items'][1]['proposed_scope']['internal_use_allowed'] = True
    with pytest.raises(ValidationError, match='not recommended'):
        ActivationPackage.model_validate(payload)


def test_candidate_limited_to_the_packaged_scope_matches(tmp_path):
    result = run(tmp_path, candidate_payload())
    assert result['status'] == 'candidate_matches_package' and result['candidate_changed_sources'] == [TWSE]
    assert result['registry_modified'] is False
    assert load_registry(REGISTRY).get(TWSE).declared_status == 'unknown'


def mutate_hash(p): twse(p)['scope']['store_response_hash'] = True
def mutate_raw(p): twse(p)['scope']['store_raw_response'] = True
def mutate_share(p): twse(p)['scope']['pdf_share_allowed'] = True
def mutate_endpoint(p): twse(p)['endpoint_scope'] = 'https://openapi.twse.com.tw/v1/exchangeReport/{dataset}'
def mutate_attribution(p): twse(p)['attribution'] = '官方資料'
def mutate_version(p): p['registry_version'] = '0.4.1'
def mutate_assistant_attestor(p): twse(p)['attested_by'] = 'Codex 互動助手'
def mutate_assistant_checker(p):
    for t in twse(p)['terms_evidence']:
        if t['checked_by'] == 'human':
            t['checker'] = 'Claude Code assistant'
def mutate_single_url(p): twse(p)['terms_evidence'].pop()
def mutate_drop_assistant_record(p): twse(p)['terms_evidence'].pop(0)
def mutate_attested_before_check(p): twse(p)['attested_at'] = '2026-11-16T08:00:00+08:00'
def mutate_remove_source(p): p['sources'] = [s for s in p['sources'] if s['source_id'] != 'yahoo-chart-daily-close']
def mutate_other_note(p): twse(p, 'sec-xbrl-companyconcept')['notes'] = 'edited'


def mutate_income(p):
    source = twse(p, INCOME)
    source.update(declared_status='confirmed', scope={'internal_use_allowed': True},
                  attested_by='Unit Test Person', attested_at='2026-11-16T10:00:00+08:00')
    source['terms_evidence'].append({**twse(p)['terms_evidence'][-1]})


@pytest.mark.parametrize('mutate, fragment', [
    (mutate_hash, 'store_response_hash'), (mutate_raw, 'store_raw_response'), (mutate_share, 'pdf_share_allowed'),
    (mutate_endpoint, 'endpoint_scope'), (mutate_attribution, '顯名'), (mutate_version, 'registry_version'),
    (mutate_assistant_attestor, '助理'), (mutate_assistant_checker, '助理'), (mutate_single_url, '逐字引用'),
    (mutate_drop_assistant_record, '既有的助理查核紀錄'), (mutate_attested_before_check, '簽認時間'),
    (mutate_remove_source, '新增或移除'), (mutate_other_note, 'sec-xbrl-companyconcept'),
    (mutate_income, INCOME),
])
def test_candidate_beyond_the_package_is_rejected(tmp_path, mutate, fragment):
    payload = candidate_payload()
    mutate(payload)
    result = run(tmp_path, payload)
    assert result['status'] == 'blocked'
    assert any(fragment in violation for violation in result['blockers'])


def test_unchanged_candidate_activates_nothing(tmp_path):
    result = run(tmp_path, read_json(REGISTRY))
    assert result['status'] == 'blocked' and any('沒有任何啟用' in v for v in result['blockers'])


def test_candidate_without_human_evidence_is_not_a_valid_registry(tmp_path, capsys):
    payload = candidate_payload()
    twse(payload)['terms_evidence'] = [t for t in twse(payload)['terms_evidence'] if t['checked_by'] != 'human']
    path = tmp_path / 'candidate.json'
    write_json(path, payload)
    with pytest.raises(ValidationError, match='human-checked quoted excerpt'):
        check(PACKAGE, load_registry(REGISTRY), instruments(), path)
    assert main(['activation-check', '--package', str(PACKAGE), '--candidate-registry', str(path)]) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'
    payload = candidate_payload()
    twse(payload).update(attested_by=None, attested_at=None)
    write_json(path, payload)
    assert main(['activation-check', '--package', str(PACKAGE), '--candidate-registry', str(path)]) == 1


def test_package_drift_blocks_the_check(tmp_path):
    registry = read_json(REGISTRY)
    registry['registry_version'] = '0.4.2'
    drifted = LicenseRegistry.model_validate(registry)
    assert check(PACKAGE, drifted, instruments())['status'] == 'blocked'
    config = copy.deepcopy(instruments())
    config['instruments'] = [i for i in config['instruments'] if i['ticker'] != '2330.TW']
    assert check(PACKAGE, load_registry(REGISTRY), config)['status'] == 'blocked'
    package = read_json(PACKAGE)
    package['items'][0]['field_binding']['close'] = ['Close']
    path = tmp_path / 'package.json'
    write_json(path, package)
    result = check(path, load_registry(REGISTRY), instruments())
    assert result['status'] == 'blocked' and any('欄位對應' in b for b in result['blockers'])
    package = read_json(PACKAGE)
    package['items'][0]['endpoint'] = 'https://openapi.twse.com.tw/v1/exchangeReport/OTHER'
    write_json(path, package)
    assert any('端點' in b for b in check(path, load_registry(REGISTRY), instruments())['blockers'])


def test_cli_reports_pending_status_without_touching_the_registry(capsys):
    before = REGISTRY.read_bytes()
    assert main(['activation-check', '--package', str(PACKAGE)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'awaiting_human_attestation' and out['target'] == '2330.TW'
    assert REGISTRY.read_bytes() == before


def test_packaged_scope_yields_only_an_insufficient_2330_report_without_raw_or_hash(tmp_path):
    """Offline rehearsal with a fake transport and a unit-test registry: not a real 2330 run."""
    registry = LicenseRegistry.model_validate(candidate_payload())
    collection = collect(TWSE, '2330.TW', registry=registry, allow_network=True,
                         client=FakeClient(payloads('twse_stock_day_all.json')), now=NOW,
                         cache_dir=tmp_path / 'cache')
    assert all('response_sha256' not in row for row in collection.transport)
    assert not (tmp_path / 'cache').exists()
    data, report = build_bundle(tsmc_request(), [collection], registry=registry)
    assert report['status'] == 'ready' and report['core_source_records'] == 1
    assert report['coverage_warnings'][0]['code'] == 'core_evidence_below_direction_threshold'
    assert sorted(e.kind for e in data.evidence) == ['identity', 'price']
    assert all(e.source_hash is None and e.internal_use_allowed and not e.shareable_in_pdf for e in data.evidence)
    item = next(i for i in read_json(PACKAGE)['items'] if i['source_id'] == TWSE)
    assert {e.attribution for e in data.evidence} == {item['proposed_attribution']}
    prepared = prepare(tsmc_request(), tmp_path, data.model_dump(mode='json'))
    run_dir = Path(prepared['research_input_path']).parent
    _, stored, _ = load_prepared(run_dir)
    # Even a directional proposal is downgraded: one licensed close cannot carry a direction.
    proposal = Proposal(input_sha256=digest(stored), model='manual-test-proposal', outlooks=[
        HorizonProposal(entity_id='2330.TW', horizon=h, outlook='bullish',
                        supporting_evidence_ids=[f'{TWSE}-2330.TW-close'], interpretation='測試用解讀。',
                        invalidation_conditions=['測試用反轉條件']) for h in ('3-6m', '6-12m')])
    write_json(run_dir / 'ai_proposal.json', proposal)
    result = finalize(run_dir, run_dir / 'ai_proposal.json', Path('config/ratings.json'))
    assert [p['outlook'] for p in read_json(run_dir / 'decision.json')['outlooks']] == ['insufficient'] * 2
    assert len(PdfReader(result['report_path']).pages) <= 2
    assert 'sha256' not in json.dumps(read_json(run_dir / 'evidence.json'))
