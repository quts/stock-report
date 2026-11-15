"""Source activation package: review-ready, grants nothing, and bounds what a review may open.

Every check runs against closed snapshot fixtures, never against the live registry, so the tests
hold whichever real sources are currently enabled."""
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
from review_fixtures import (CLOSED, CLOSED_INSTRUMENTS, CLOSED_PACKAGE, assistant_review, finding,
                             source_of)

PACKAGE = Path(CLOSED_PACKAGE)
REGISTRY = Path(CLOSED)
INCOME = 'twse-openapi-income-general'


def instruments():
    return read_json(CLOSED_INSTRUMENTS)


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


def reviewed_payload():
    """A test-only stand-in for the edit an authorized assistant would make. Never written to config/."""
    payload = read_json(REGISTRY)
    item = next(i for i in read_json(PACKAGE)['items'] if i['source_id'] == TWSE)
    payload['registry_version'] = '0.5.0'
    source = source_of(payload, TWSE)
    source.update(declared_status='confirmed', scope=item['proposed_scope'], attribution=item['proposed_attribution'])
    source['source_review'] = assistant_review(source, extra_urls=[item['license_url']])
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
    assert result['status'] == 'awaiting_source_review' and result['registry_modified'] is False
    # The historical package keeps its own wording; it is read as awaiting a source review.
    assert result['package_status'] == 'awaiting_human_attestation'
    assert result['accepted_review_bases'] == ['human_attestation', 'assistant_source_review']
    assert result['package_drift'] == [] and result['required_review_actions']
    assert {i['source_id']: i['registry_status'] for i in result['items']} == {TWSE: 'unknown', INCOME: 'unknown'}
    assert REGISTRY.read_bytes() == before
    registry = load_registry(REGISTRY)
    assert not any(registry.snapshot(s.source_id)['internal_use_allowed'] for s in registry.sources)
    package = read_json(PACKAGE)
    assert package['human_attestation'] is None and package['prepared_by_kind'] == 'assistant'


def test_the_live_package_and_registry_still_parse():
    """Structure only: which sources are enabled is configuration, not a test expectation."""
    ActivationPackage.model_validate(read_json('docs/phase-0/2330-source-activation.json'))
    registry = load_registry('config/source-licenses.json')
    for source in registry.sources:
        snapshot = registry.snapshot(source.source_id)
        assert snapshot['source_reviewed'] == (snapshot['review_basis'] is not None)
        if snapshot['status'] != 'confirmed':
            assert not snapshot['internal_use_allowed'] and not snapshot['source_reviewed']
        if snapshot['review_basis'] == 'assistant_source_review':
            assert not snapshot['human_attested'] and snapshot['attested_by'] is None
            assert not (snapshot['pdf_share_allowed'] or snapshot['store_response_hash']
                        or snapshot['store_raw_response'])


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
    payload['source_review'] = {'reviewed_by': 'assistant'}
    with pytest.raises(ValidationError):
        ActivationPackage.model_validate(payload)
    payload = read_json(PACKAGE)
    payload.update(status='awaiting_source_review')
    payload['items'][0].update(recommendation='ready_for_source_review',
                               review_checklist=payload['items'][0].pop('human_checklist'))
    assert ActivationPackage.model_validate(payload).items[0].checklist()
    payload = read_json(PACKAGE)
    payload['items'][1]['proposed_scope']['internal_use_allowed'] = True
    with pytest.raises(ValidationError, match='not recommended'):
        ActivationPackage.model_validate(payload)


def test_candidate_limited_to_the_packaged_scope_matches(tmp_path):
    result = run(tmp_path, candidate_payload())
    assert result['status'] == 'candidate_matches_package' and result['candidate_changed_sources'] == [TWSE]
    assert result['candidate_review_basis'] == {TWSE: 'human_attestation'}
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
    with pytest.raises(ValidationError, match='human sign-off'):
        check(PACKAGE, load_registry(REGISTRY), instruments(), path)
    assert main(['activation-check', '--package', str(PACKAGE), '--instruments', CLOSED_INSTRUMENTS,
                 '--candidate-registry', str(path)]) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'
    payload = candidate_payload()
    twse(payload).update(attested_by=None, attested_at=None)
    write_json(path, payload)
    assert main(['activation-check', '--package', str(PACKAGE), '--instruments', CLOSED_INSTRUMENTS,
                 '--candidate-registry', str(path)]) == 1


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
    assert main(['activation-check', '--package', str(PACKAGE), '--instruments', CLOSED_INSTRUMENTS]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'awaiting_source_review' and out['target'] == '2330.TW'
    assert REGISTRY.read_bytes() == before


def test_packaged_scope_yields_only_an_insufficient_2330_report_without_raw_or_hash(tmp_path):
    """Offline rehearsal with a fake transport and a unit-test registry: not a real 2330 run."""
    registry = LicenseRegistry.model_validate(candidate_payload())
    collection = collect(TWSE, '2330.TW', registry=registry, allow_network=True,
                         client=FakeClient(payloads('twse_stock_day_all.json')), now=NOW,
                         cache_dir=tmp_path / 'cache')
    assert all('response_sha256' not in row for row in collection.transport)
    assert not (tmp_path / 'cache').exists()
    # Legacy two-page profile: the four-page profile additionally requires the latest filing.
    request = tsmc_request().model_copy(update={'max_pdf_pages': 2})
    data, report = build_bundle(request, [collection], registry=registry)
    assert report['status'] == 'ready' and report['core_source_records'] == 1
    assert report['coverage_warnings'][0]['code'] == 'core_evidence_below_direction_threshold'
    assert sorted(e.kind for e in data.evidence) == ['identity', 'price']
    assert all(e.source_hash is None and e.internal_use_allowed and not e.shareable_in_pdf for e in data.evidence)
    item = next(i for i in read_json(PACKAGE)['items'] if i['source_id'] == TWSE)
    assert {e.attribution for e in data.evidence} == {item['proposed_attribution']}
    prepared = prepare(request, tmp_path, data.model_dump(mode='json'))
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


# --- assistant source review under user authorization --------------------------------------

def test_assistant_reviewed_candidate_matches_and_is_never_a_human_attestation(tmp_path):
    result = run(tmp_path, reviewed_payload())
    assert result['status'] == 'candidate_matches_package' and result['candidate_changed_sources'] == [TWSE]
    assert result['candidate_review_basis'] == {TWSE: 'assistant_source_review'}
    assert any('不是人工簽認' in w for w in result['warnings'])
    snapshot = LicenseRegistry.model_validate(reviewed_payload()).snapshot(TWSE)
    assert snapshot['source_reviewed'] and snapshot['review_basis'] == 'assistant_source_review'
    assert not snapshot['human_attested'] and snapshot['attested_by'] is None
    assert snapshot['internal_use_allowed'] and not snapshot['store_response_hash']
    assert {f['aspect'] for f in snapshot['source_review']['findings']} >= {
        'official_source', 'identity', 'publication_time', 'terms_of_use', 'free_access'}


def review(p): return twse(p)['source_review']
def drop_aspect(aspect):
    def mutate(p): review(p)['findings'] = [f for f in review(p)['findings'] if f['aspect'] != aspect]
    return mutate
def mutate_unresolved(p): review(p)['unresolved'] = ['端點與資料集的對應未有官方明示']
def mutate_insufficient(p): review(p)['verdict'] = 'insufficient'
def mutate_http(p): review(p)['findings'][0]['official_url'] = 'http://data.gov.tw/dataset/11549'
def mutate_other_scope(p): review(p)['reviewed_endpoint_scope'] = 'https://openapi.twse.com.tw/v1/other'
def mutate_review_before_findings(p): review(p)['reviewed_at'] = '2026-11-16T08:00:00+08:00'
def mutate_no_authorization(p): review(p)['authorization'] = ''
def mutate_reviewed_by_human(p): review(p)['reviewed_by'] = 'human'
def mutate_fake_attestor(p): twse(p).update(attested_by='Codex 互動助手', attested_at='2026-11-16T10:00:00+08:00')
def mutate_unquoted_terms(p): review(p)['findings'].append(finding('terms_of_use', 'https://example.invalid/unquoted'))
def mutate_review_hash(p): twse(p)['scope']['store_response_hash'] = True
def mutate_review_raw(p): twse(p)['scope']['store_raw_response'] = True
def mutate_review_share(p): twse(p)['scope']['pdf_share_allowed'] = True


@pytest.mark.parametrize('mutate, message', [
    (drop_aspect('official_source'), 'official finding'), (drop_aspect('identity'), 'official finding'),
    (drop_aspect('publication_time'), 'official finding'), (drop_aspect('free_access'), 'official finding'),
    (drop_aspect('terms_of_use'), 'official finding'), (mutate_unresolved, 'unresolved'),
    (mutate_insufficient, 'human-checked quoted excerpt'), (mutate_http, 'official_url'),
    (mutate_other_scope, 'never transfers'), (mutate_review_before_findings, 'dated before'),
    (mutate_no_authorization, 'authorization'), (mutate_reviewed_by_human, 'reviewed_by'),
    (mutate_fake_attestor, 'human sign-off'), (mutate_unquoted_terms, 'quoted'),
    (mutate_review_hash, 'need a human attestation'), (mutate_review_raw, 'need a human attestation'),
    (mutate_review_share, 'need a human attestation'),
])
def test_insufficient_or_disguised_assistant_review_is_not_a_valid_registry(tmp_path, mutate, message):
    payload = reviewed_payload()
    mutate(payload)
    with pytest.raises(ValidationError, match=message):
        run(tmp_path, payload)


def mutate_review_misses_license_page(p):
    license_url = next(i for i in read_json(PACKAGE)['items'] if i['source_id'] == TWSE)['license_url']
    review(p)['findings'] = [f for f in review(p)['findings'] if f['official_url'] != license_url]
def mutate_review_drops_quote(p):
    license_url = next(i for i in read_json(PACKAGE)['items'] if i['source_id'] == TWSE)['license_url']
    review(p)['findings'] = [f for f in review(p)['findings'] if f['official_url'] != license_url]
    review(p)['findings'].append({**finding('free_access', license_url)})
    twse(p)['terms_evidence'] = [t for t in twse(p)['terms_evidence'] if t['terms_url'] != license_url]
def mutate_review_with_fake_human_row(p):
    twse(p)['terms_evidence'].append({'terms_url': 'https://data.gov.tw/license', 'checked_at': '2026-11-16T09:00:00+08:00',
                                      'checked_by': 'human', 'checker': 'Claude Code assistant',
                                      'excerpt': 'UNIT TEST TEXT', 'excerpt_covers': 'unit test only'})
def mutate_review_income(p):
    source = twse(p, INCOME)
    source.update(declared_status='confirmed', scope={'internal_use_allowed': True},
                  source_review=assistant_review(source))


@pytest.mark.parametrize('mutate, fragment', [
    (mutate_review_misses_license_page, '官方依據'), (mutate_review_drops_quote, '既有的助理查核紀錄'),
    (mutate_review_with_fake_human_row, '不能視為人工簽認'), (mutate_review_income, INCOME),
    (mutate_endpoint, 'endpoint_scope'), (mutate_attribution, '顯名'), (mutate_version, 'registry_version'),
    (mutate_other_note, 'sec-xbrl-companyconcept'),
])
def test_assistant_reviewed_candidate_beyond_the_package_is_rejected(tmp_path, mutate, fragment):
    payload = reviewed_payload()
    mutate(payload)
    if mutate is mutate_endpoint:
        review(payload)['reviewed_endpoint_scope'] = twse(payload)['endpoint_scope']
    result = run(tmp_path, payload)
    assert result['status'] == 'blocked'
    assert any(fragment in violation for violation in result['blockers'])


def test_assistant_reviewed_source_feeds_a_bundle_marked_by_its_basis(tmp_path):
    """Offline rehearsal with a fake transport and authored review records: not a real 2330 run."""
    registry = LicenseRegistry.model_validate(reviewed_payload())
    collection = collect(TWSE, '2330.TW', registry=registry, allow_network=True,
                         client=FakeClient(payloads('twse_stock_day_all.json')), now=NOW,
                         cache_dir=tmp_path / 'cache')
    assert collection.retention == 'research_input_allowed'
    assert collection.license['review_basis'] == 'assistant_source_review' and not collection.license['human_attested']
    assert all('response_sha256' not in row for row in collection.transport) and not (tmp_path / 'cache').exists()
    data, report = build_bundle(tsmc_request(), [collection], registry=registry)
    assert report['status'] == 'ready'
    row = report['collections'][0]
    assert row['source_reviewed'] and row['review_basis'] == 'assistant_source_review' and not row['human_attested']
    assert all(e.verification_status == 'verified' and e.source_hash is None and not e.shareable_in_pdf
               for e in data.evidence)
    # The same capture against the closed registry is excluded: the stricter snapshot wins.
    closed = load_registry(REGISTRY)
    data, report = build_bundle(tsmc_request(), [collection], registry=closed)
    assert data is None and {r['reason'] for r in report['exclusions']} == {'license_not_confirmed_for_internal_use'}
    # A capture recorded while the source was closed gains nothing when the registry opens later.
    early = collect(TWSE, '2330.TW', registry=closed, allow_network=True,
                    client=FakeClient(payloads('twse_stock_day_all.json')), now=NOW)
    data, report = build_bundle(tsmc_request(), [early], registry=registry)
    assert data is None and not report['collections'][0]['source_reviewed']


def test_package_against_an_already_reviewed_registry_reports_its_basis(tmp_path):
    payload = reviewed_payload()
    payload['registry_version'] = read_json(REGISTRY)['registry_version']  # same baseline: no drift
    result = check(PACKAGE, LicenseRegistry.model_validate(payload), instruments())
    assert result['status'] == 'registry_already_reviewed'
    assert result['registry_review_basis'] == {TWSE: 'assistant_source_review'}
