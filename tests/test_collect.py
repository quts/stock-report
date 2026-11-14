"""Collection, licensing and cutoff contract tests. No network, no mock fallback."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.error import HTTPError
import pytest
from pydantic import ValidationError
from stock_report.cli import main
from stock_report.collect import (SEC_CONTACT_ENV, build_bundle, collect, load_collection, persist,
                                  request_template, resolve_sec_contact, to_evidence, user_agent_for,
                                  write_collection)
from stock_report.licensing import LicenseRegistry, load_registry
from stock_report.models import HorizonProposal, Proposal, Request
from stock_report.pipeline import finalize, prepare
from stock_report.policy import evaluate
from stock_report.storage import digest, read_json, write_json

FIXTURES = Path('tests/fixtures')
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
TWSE = 'twse-openapi-stock-day-all'
SEC = 'sec-xbrl-companyconcept'


@pytest.fixture
def registry():
    return load_registry('config/source-licenses.json')


@pytest.fixture
def attested():
    return load_registry(FIXTURES / 'licenses-attested.json')


class FakeClient:
    """Deterministic offline transport; never falls back to any other data."""

    def __init__(self, data, fail=None, response_sha256='b' * 64):
        self.data, self.fail, self.response_sha256, self.calls = data, fail, response_sha256, []

    def get_json(self, url):
        self.calls.append(url)
        if self.fail:
            raise self.fail
        key = next((k for k in self.data if k == 'daily' or url.endswith(k.split(':')[-1] + '.json')), None)
        if key is None:
            raise HTTPError(url, 404, 'not found', {}, None)
        return self.data[key], {'attempts': 1, 'cached': False, 'response_sha256': self.response_sha256}


def payloads(name):
    return read_json(FIXTURES / name)


def twse_collection(registry, **kwargs):
    return collect(TWSE, '2330.TW', registry=registry, allow_network=True,
                   client=FakeClient(payloads('twse_stock_day_all.json')), now=NOW, **kwargs)


def sec_collection(registry, **kwargs):
    return collect(SEC, 'NVDA', registry=registry, allow_network=True,
                   client=FakeClient(payloads('sec_nvda_concepts.json')), now=NOW, **kwargs)


def nvda_request(as_of=NOW):
    return Request(mode='stock', ticker='NVDA', exchange='NASDAQ', research_as_of=as_of)


def tsmc_request(as_of=NOW):
    return Request(mode='stock', ticker='2330.TW', exchange='TWSE', research_as_of=as_of)


def test_observed_availability_is_stamped_after_the_response_completes(registry):
    """Deterministic injected clock: the upper bound may not predate transport completion."""
    ticks = [NOW]

    class DelayedClient(FakeClient):
        def get_json(self, url):
            ticks.append(ticks[-1] + timedelta(seconds=5))
            return super().get_json(url)

    collection = collect(TWSE, '2330.TW', registry=registry, allow_network=True,
                         client=DelayedClient(payloads('twse_stock_day_all.json')), clock=lambda: ticks[-1])
    assert collection.collected_at == NOW + timedelta(seconds=5)
    assert all(o.published_at == NOW + timedelta(seconds=5) for o in collection.observations)
    assert all(o.availability_observed_at == collection.collected_at for o in collection.observations)


def test_unknown_internal_license_cannot_persist_source_values(registry, tmp_path):
    collection = twse_collection(registry)
    assert not collection.license['internal_use_allowed']
    with pytest.raises(ValueError, match='不得寫入任何來源數值'):
        write_collection(collection, tmp_path)
    assert not list(tmp_path.rglob('collection.json'))
    path, artifact = persist(collection, tmp_path)
    assert artifact == 'inspection_record' and path.name == 'inspection-record.json'
    record = read_json(path)
    text = path.read_text()
    assert record['blockers'][0]['code'] == 'source_values_not_cleared_for_local_persistence'
    assert record['field_binding']['close'] == 'ClosingPrice'
    assert all('value' not in key for key in record['observations'][0] if key != 'value_present')
    # No source values, source-provided names or provider record identifiers are retained.
    assert '100.00' not in text and '台積電' not in text and '2330@1151113' not in text


def test_fixture_captures_may_be_persisted_but_never_promoted_to_real(attested, tmp_path):
    collection = collect(TWSE, '2330.TW', registry=attested, fixture=FIXTURES / 'twse_stock_day_all.json', now=NOW)
    path, artifact = persist(collection, tmp_path)
    assert artifact == 'collection' and path.name == 'collection.json'
    data, report = build_bundle(tsmc_request(), [collection], registry=attested)
    assert data is None
    assert 'fixture_capture_not_real_evidence' in {b['code'] for b in report['blockers']}
    # Even with the offline opt-in, the bundle is labelled mock and its evidence stays synthetic,
    # so the import path refuses it instead of treating authored content as official.
    data, report = build_bundle(tsmc_request(), [collection], registry=attested, allow_fixture_capture=True)
    assert report['dataset_kind'] == 'mock' and data.dataset_kind == 'mock'
    assert all(e.source_type == 'synthetic' for e in data.evidence)
    with pytest.raises(ValueError, match='reserved for explicitly reviewed real evidence'):
        prepare(tsmc_request(), tmp_path / 'runs', data.model_dump(mode='json'))


def test_endpoint_change_does_not_inherit_reviewed_dataset_permissions(attested, tmp_path):
    with pytest.raises(ValueError, match='outside the reviewed scope'):
        collect(TWSE, '2330.TW', registry=attested, allow_network=True, now=NOW,
                client=FakeClient(payloads('twse_stock_day_all.json')),
                profile_overrides={'endpoint': 'https://example.invalid/unlicensed-dataset'})
    collection = twse_collection(attested)
    moved = collection.model_copy(update={'endpoint': 'https://example.invalid/unlicensed-dataset'})
    data, report = build_bundle(tsmc_request(), [moved], registry=attested)
    assert data is None
    assert 'endpoint_outside_license_scope' in {b['code'] for b in report['blockers']}
    assert collection.license['endpoint_scope'].startswith('https://openapi.twse.com.tw/')
    # Record URLs inside a loaded collection are re-bound to the reviewed dataset as well.
    repointed = collection.model_copy(deep=True)
    for observation in repointed.observations:
        observation.source_url = 'https://example.invalid/unlicensed-dataset'
    data, report = build_bundle(tsmc_request(), [repointed], registry=attested)
    assert data is None
    assert 'evidence_url_outside_license_scope' in {b['code'] for b in report['blockers']}
    # The SEC concept template still matches the concrete per-concept URLs it was reviewed for.
    sec = sec_collection(attested)
    assert build_bundle(nvda_request(), [sec], registry=attested)[0] is not None


def test_licensed_single_quote_produces_an_insufficient_report(attested, tmp_path):
    """Core-record count is the directional Gate threshold, not a condition for any report."""
    collection = twse_collection(attested)
    request = tsmc_request()
    data, report = build_bundle(request, [collection], registry=attested)
    assert data is not None and report['status'] == 'ready'
    assert report['core_source_records'] == 1
    assert {w['code'] for w in report['coverage_warnings']} == {'core_evidence_below_direction_threshold'}
    prepared = prepare(request, tmp_path, data.model_dump(mode='json'))
    run = Path(prepared['research_input_path']).parent
    proposal = Proposal(input_sha256=digest(data), model='manual-test-proposal', outlooks=[
        HorizonProposal(entity_id='2330.TW', horizon=horizon, outlook='bullish',
                        supporting_evidence_ids=[f'{TWSE}-2330.TW-close'], counter_evidence_ids=[],
                        interpretation='測試用解讀；只有一筆收盤價，不足以支撐方向。',
                        invalidation_conditions=['取得財報與第二個獨立來源後重新研究'])
        for horizon in ('3-6m', '6-12m')])
    write_json(run / 'ai_proposal.json', proposal)
    result = finalize(run, run / 'ai_proposal.json', Path('config/ratings.json'))
    decision = read_json(run / 'decision.json')
    assert result['gate_status'] == 'REJECTED'
    assert [p['outlook'] for p in decision['outlooks']] == ['insufficient', 'insufficient']
    assert 'CORE_INSUFFICIENT' in {f['rule_id'] for f in decision['findings']}
    assert Path(result['report_path']).is_file() and read_json(run / 'manifest.json')['pdf_pages'] == 2


def test_collection_requires_explicit_network_opt_in(registry):
    with pytest.raises(ValueError, match='allow-network'):
        collect(TWSE, '2330.TW', registry=registry)


def test_network_failure_raises_and_never_substitutes_data(registry):
    client = FakeClient(payloads('twse_stock_day_all.json'), fail=TimeoutError('simulated outage'))
    with pytest.raises(TimeoutError):
        collect(TWSE, '2330.TW', registry=registry, allow_network=True, client=client, now=NOW)


def test_sec_optional_concept_404_is_recorded_not_fatal(registry):
    data = {k: v for k, v in payloads('sec_nvda_concepts.json').items() if not k.startswith('eps:')}
    collection = collect(SEC, 'NVDA', registry=registry, allow_network=True, client=FakeClient(data), now=NOW)
    unavailable = [row for row in collection.transport if row['status'] == 'unavailable']
    assert any(row['key'].startswith('eps:') for row in unavailable)
    assert all(o.role not in ('eps_current', 'eps_prior') for o in collection.observations)


def test_fixture_capture_is_labeled_and_refused_as_real_evidence(registry, attested):
    collection = collect(TWSE, '2330.TW', registry=attested, fixture=FIXTURES / 'twse_stock_day_all.json', now=NOW)
    assert collection.capture_mode == 'fixture' and collection.transport == [{'key': 'daily', 'status': 'fixture'}]
    data, report = build_bundle(tsmc_request(), [collection], registry=attested)
    assert data is None
    assert 'fixture_capture_not_real_evidence' in {b['code'] for b in report['blockers']}
    data, report = build_bundle(tsmc_request(), [collection], registry=attested, allow_fixture_capture=True)
    assert 'fixture_capture_not_real_evidence' not in {b['code'] for b in report['blockers']}


def test_fixture_keys_outside_the_source_contract_are_refused(registry, tmp_path):
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps({'daily': [], 'other': []}))
    with pytest.raises(ValueError, match='outside this source contract'):
        collect(TWSE, '2330.TW', registry=registry, fixture=path, now=NOW)


def test_unknown_license_keeps_everything_out_of_the_bundle(registry):
    collection = twse_collection(registry)
    assert collection.license['status'] == 'unknown'
    assert not collection.license['internal_use_allowed'] and not collection.license['pdf_share_allowed']
    assert collection.retention == 'feasibility_only'
    assert any('不得作為研究輸入' in note for note in collection.notes)
    data, report = build_bundle(tsmc_request(), [collection], registry=registry)
    assert data is None and report['status'] == 'blocked'
    assert {row['reason'] for row in report['exclusions']} == {'license_not_confirmed_for_internal_use'}
    assert {'identity_evidence_unavailable',
            'collection_contributed_no_usable_evidence'} <= {b['code'] for b in report['blockers']}
    assert report['kept_evidence'] == []


def test_registry_drift_applies_the_stricter_of_both_snapshots(registry, attested):
    collection = twse_collection(attested)
    assert collection.license['status'] == 'confirmed'
    data, report = build_bundle(tsmc_request(), [collection], registry=registry)
    assert data is None
    assert any('授權登錄檔版本或狀態與採集時不同' in note for note in report['notes'])
    assert {row['reason'] for row in report['exclusions']} == {'license_not_confirmed_for_internal_use'}


def test_core_record_shortfall_is_a_coverage_warning_not_a_blocker(attested):
    collection = twse_collection(attested)
    data, report = build_bundle(tsmc_request(), [collection], registry=attested)
    assert data is not None
    assert report['core_source_records'] == 1 and report['direction_threshold'] == 2
    assert report['blockers'] == []
    assert 'core_evidence_below_direction_threshold' in {w['code'] for w in report['coverage_warnings']}


def test_attested_sec_collection_builds_a_real_bundle_and_prepares(attested, tmp_path):
    collection = sec_collection(attested)
    request = nvda_request()
    assert collection.retention == 'research_input_allowed'
    data, report = build_bundle(request, [collection], registry=attested)
    assert report['status'] == 'ready' and data is not None
    assert data.dataset_kind == 'real' and data.versions['dataset'] == 'real-collected-1'
    assert all(e.source_type == 'official' and e.verification_status == 'verified' for e in data.evidence)
    assert all(e.license_source_id == SEC and e.license_registry_version == '9.9.9' for e in data.evidence)
    metrics = {m.name: m for m in data.metrics}
    assert metrics['revenue_growth'].value == pytest.approx(23.45, abs=1e-6)
    assert metrics['net_margin'].value == pytest.approx(19.44107, abs=1e-4)
    assert metrics['free_cash_flow'].value == 220000000
    assert metrics['pe'].value is None and metrics['pe'].missing_reason
    result = prepare(request, tmp_path, data.model_dump(mode='json'))
    assert result['coverage']['NVDA']['financial'] == 'available'
    assert result['coverage']['NVDA']['price'] == 'unavailable'
    assert result['warnings'] == ['Real evidence import requires human source/license verification']
    assert read_json(Path(result['research_input_path']))['dataset_kind'] == 'real'


def test_real_evidence_passes_through_the_gate_without_bypass(attested):
    data, _ = build_bundle(nvda_request(), [sec_collection(attested)], registry=attested)
    proposal = Proposal(input_sha256=digest(data), model='manual-test-proposal', outlooks=[
        HorizonProposal(entity_id='NVDA', horizon=horizon, outlook='bullish',
                        supporting_evidence_ids=['sec-NVDA-revenue_current', 'sec-NVDA-net_income'],
                        counter_evidence_ids=[], interpretation='測試用解讀，僅驗證 Gate 流程。',
                        invalidation_conditions=['年度營收或淨利轉為衰退時重新研究'])
        for horizon in ('3-6m', '6-12m')])
    decision = evaluate(data, proposal)
    rules = {f.rule_id for f in decision.findings}
    assert decision.status == 'APPROVED_WITH_WARNINGS'
    assert [p.outlook for p in decision.outlooks] == ['bullish', 'bullish']
    assert 'SINGLE_SOURCE' in rules and 'RATINGS_MISSING' in rules
    assert not {'LICENSE', 'FUTURE_DATA', 'UNVERIFIED', 'QUANT_MISMATCH'} & rules


def test_historical_cutoff_excludes_observed_availability_and_is_never_rewritten(attested):
    collection = twse_collection(attested)
    request = tsmc_request(NOW - timedelta(days=10))
    data, report = build_bundle(request, [collection], registry=attested)
    assert data is None
    assert request.research_as_of == NOW - timedelta(days=10)
    assert report['request']['research_as_of'] == (NOW - timedelta(days=10)).isoformat()
    assert {row['reason'] for row in report['exclusions']} == {
        'historical_as_of_not_supported_by_observed_availability'}
    assert all(row['publication_basis'] == 'observed_availability' for row in report['exclusions'])


def test_declared_filing_dates_survive_a_historical_cutoff(attested):
    """SEC filings carry a source-declared date, so a historical run keeps them."""
    request = nvda_request(datetime(2025, 6, 30, tzinfo=timezone.utc))
    data, report = build_bundle(request, [sec_collection(attested)], registry=attested)
    assert report['status'] == 'ready' and data is not None
    assert all(e.publication_basis == 'source_declared_date' for e in data.evidence)


def test_evidence_published_after_the_cutoff_is_excluded(attested):
    request = nvda_request(datetime(2024, 6, 30, tzinfo=timezone.utc))
    data, report = build_bundle(request, [sec_collection(attested)], registry=attested)
    assert data is None
    assert 'published_after_research_as_of' in {row['reason'] for row in report['exclusions']}


def test_response_hash_is_stored_only_when_the_registry_allows_it(attested):
    twse = twse_collection(attested)
    sec = sec_collection(attested)
    assert twse.license['store_response_hash'] and not sec.license['store_response_hash']
    assert twse.transport[0]['response_sha256'] == 'b' * 64
    assert all('response_sha256' not in row for row in sec.transport)
    assert any('已略過 source_hash' in note for note in sec.notes)
    allowed = to_evidence(twse.observations[0], tsmc_request(), twse.license, 'network', True, 'b' * 64)
    refused = to_evidence(sec.observations[0], nvda_request(), sec.license, 'network', True, 'b' * 64)
    assert allowed.source_hash == 'b' * 64 and refused.source_hash is None


def test_raw_response_cache_is_disabled_without_permission(attested, tmp_path, monkeypatch):
    captured = {}

    class Recorder(FakeClient):
        def __init__(self, user_agent, **kwargs):
            super().__init__(payloads('twse_stock_day_all.json'))
            captured.update(user_agent=user_agent, **kwargs)

    monkeypatch.setattr('stock_report.collect.HTTPClient', Recorder)
    collection = collect(TWSE, '2330.TW', registry=attested, allow_network=True, now=NOW, cache_dir=tmp_path)
    assert captured['cache_dir'] is None and captured['user_agent'].startswith('stock-report-phase0')
    assert any('停用原始回應快取' in note for note in collection.notes)
    assert not any(tmp_path.iterdir())


def test_sec_contact_must_be_configured_explicitly(monkeypatch):
    monkeypatch.delenv(SEC_CONTACT_ENV, raising=False)
    with pytest.raises(ValueError, match=SEC_CONTACT_ENV):
        user_agent_for(SEC, environ={})
    assert user_agent_for(TWSE, environ={}) == user_agent_for(TWSE)
    agent = user_agent_for(SEC, environ={SEC_CONTACT_ENV: 'Research Ops ops@example.invalid'})
    assert agent.endswith('ops@example.invalid') and '\n' not in agent


@pytest.mark.parametrize('value', ['ops@example.invalid\nX-Injected: 1', 'no-at-sign', 'x' * 130 + '@a.invalid'])
def test_bad_sec_contact_is_rejected_without_echoing_the_value(value):
    with pytest.raises(ValueError) as error:
        resolve_sec_contact(value, environ={})
    assert value not in str(error.value)


def test_collection_artifacts_never_contain_the_contact(attested, tmp_path, monkeypatch):
    monkeypatch.setenv(SEC_CONTACT_ENV, 'Research Ops ops@example.invalid')
    collection = sec_collection(attested)
    directory = write_collection(collection, tmp_path)
    text = (directory / 'collection.json').read_text()
    assert 'ops@example.invalid' not in text
    assert collection.sec_contact_configured is True
    assert load_collection(directory).collection_id == collection.collection_id


def test_entity_exchange_and_cross_source_identity_are_checked(attested):
    sec = sec_collection(attested)
    twse = twse_collection(attested)
    data, report = build_bundle(tsmc_request(), [sec], registry=attested)
    assert data is None
    assert 'collection_ticker_mismatch' in {b['code'] for b in report['blockers']}
    data, report = build_bundle(nvda_request(), [sec, twse], registry=attested)
    assert data is None
    assert 'collection_ticker_mismatch' in {b['code'] for b in report['blockers']}
    request = Request(mode='stock', ticker='NVDA', exchange='NYSE', research_as_of=NOW)
    data, report = build_bundle(request, [sec], registry=attested)
    assert data is None
    assert 'request_exchange_mismatch' in {b['code'] for b in report['blockers']}


def test_event_mode_is_not_served_by_single_company_collection(attested):
    request = Request(mode='event', event_query='測試事件', research_as_of=NOW)
    data, report = build_bundle(request, [sec_collection(attested)], registry=attested)
    assert data is None
    assert 'event_mode_real_collection_not_implemented' in {b['code'] for b in report['blockers']}


def test_request_template_requires_a_cutoff_after_collection(attested):
    collection = twse_collection(attested)
    template = request_template('stock', '2330.TW', 'TWSE', [collection])
    assert template.research_as_of == NOW
    later = request_template('stock', '2330.TW', 'TWSE', [collection], NOW + timedelta(hours=2))
    assert later.research_as_of == NOW + timedelta(hours=2)
    with pytest.raises(ValueError, match='collect first'):
        request_template('stock', '2330.TW', 'TWSE', [collection], NOW - timedelta(days=1))


def test_default_registry_grants_nothing(registry):
    assert registry.registry_version == '0.1.0'
    for source in registry.sources:
        snapshot = registry.snapshot(source.source_id)
        assert snapshot['status'] == 'unknown' and not snapshot['human_attested']
        assert not any(snapshot[name] for name in ('internal_use_allowed', 'pdf_share_allowed',
                                                   'store_response_hash', 'store_raw_response'))
        assert snapshot['open_questions']
    with pytest.raises(ValueError, match='known sources'):
        registry.snapshot('some-paid-vendor')


def test_confirmed_terms_require_quoted_human_evidence_and_attestation(attested):
    payload = attested.model_dump(mode='json')
    payload['sources'][0]['terms_evidence'] = []
    with pytest.raises(ValidationError, match='human-checked quoted excerpt'):
        LicenseRegistry.model_validate(payload)
    payload = attested.model_dump(mode='json')
    payload['sources'][0]['attested_by'] = None
    with pytest.raises(ValidationError, match='named attestation'):
        LicenseRegistry.model_validate(payload)
    payload = attested.model_dump(mode='json')
    payload['sources'][2]['scope']['pdf_share_allowed'] = True
    with pytest.raises(ValidationError, match='confirmed, attested entry'):
        LicenseRegistry.model_validate(payload)


def test_cli_collect_then_bundle_reports_blockers_and_then_prepares(tmp_path, capsys):
    collections = tmp_path / 'collections'
    assert main(['collect', '--source', SEC, '--ticker', 'NVDA', '--fixture',
                 str(FIXTURES / 'sec_nvda_concepts.json'), '--output', str(collections)]) == 0
    unlicensed = json.loads(capsys.readouterr().out)
    assert unlicensed['status'] == 'collected' and unlicensed['license_status'] == 'unknown'
    assert unlicensed['capture_mode'] == 'fixture' and unlicensed['persisted'] == 'collection'
    assert main(['collect', '--source', SEC, '--ticker', 'NVDA', '--fixture',
                 str(FIXTURES / 'sec_nvda_concepts.json'), '--output', str(collections),
                 '--license-registry', str(FIXTURES / 'licenses-attested.json')]) == 0
    collected = json.loads(capsys.readouterr().out)
    assert collected['license_status'] == 'confirmed'
    run = Path(collected['collection_path']).parent
    request = tmp_path / 'request.json'
    assert main(['request-template', '--ticker', 'NVDA', '--exchange', 'NASDAQ', '--collection', str(run),
                 '--output', str(request)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'success'
    assert main(['request-template', '--ticker', 'NVDA', '--collection', str(run), '--output', str(request)]) == 1
    assert 'Refusing to overwrite' in json.loads(capsys.readouterr().out)['message']
    bundle = tmp_path / 'bundle.json'
    assert main(['bundle', '--request', str(request), '--collection', str(run), '--output', str(bundle),
                 '--allow-fixture-capture']) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked['status'] == 'blocked' and not bundle.exists()
    assert 'license_not_confirmed_for_internal_use' in blocked['excluded']
    assert main(['bundle', '--request', str(request), '--collection', str(run), '--output', str(bundle),
                 '--allow-fixture-capture', '--license-registry', str(FIXTURES / 'licenses-attested.json')]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready['status'] == 'ready' and Path(ready['bundle_path']).is_file()
    # The offline opt-in produces a mock-labelled bundle, and the import path still refuses it.
    assert ready['dataset_kind'] == 'mock'
    assert read_json(bundle)['dataset_kind'] == 'mock'
    assert main(['prepare', '--request', str(request), '--provider', 'import', '--evidence-bundle', str(bundle),
                 '--output', str(tmp_path / 'runs')]) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused['status'] == 'failed' and 'real evidence bundles' in refused['message']


def test_cli_collect_refuses_network_and_fixture_together(tmp_path, capsys):
    assert main(['collect', '--source', TWSE, '--ticker', '2330.TW', '--allow-network', '--fixture',
                 str(FIXTURES / 'twse_stock_day_all.json'), '--output', str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)['error_type'] == 'ValueError'


def test_cli_network_inspection_does_not_echo_unapproved_company_name(tmp_path, capsys, monkeypatch, registry):
    import stock_report.cli as cli_module
    # Existing source-shaped synthetic fixture is injected into a network capture contract.
    payload = read_json(FIXTURES / 'twse_stock_day_all.json')
    class FixtureTransport:
        def get_json(self,url):return payload['daily'], {'attempts':1,'cached':False}
    collection=collect(TWSE,'2330.TW',registry=registry,allow_network=True,
                       client=FixtureTransport(),now=NOW)
    monkeypatch.setattr(cli_module,'collect',lambda *args,**kwargs:collection)
    assert main(['collect','--source',TWSE,'--ticker','2330.TW','--allow-network','--output',str(tmp_path)])==0
    output=capsys.readouterr().out
    assert collection.entity.name not in output
    assert json.loads(output)['status']=='inspection_only'
