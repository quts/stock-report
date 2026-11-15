"""SEC targets in activation-check: CIK, entity, market, concept, unit and endpoint binding.

Everything runs against the closed registry snapshot and a synthetic package fixture. Nothing
here reviews or activates a real source, and nothing is written to config/."""
import copy
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from stock_report.activation import ActivationPackage, check
from stock_report.cli import main
from stock_report.licensing import load_registry
from stock_report.sources import PROFILES, SecConceptAdapter, merge_profile
from stock_report.storage import read_json, write_json
from review_fixtures import CLOSED, CLOSED_INSTRUMENTS, CLOSED_PACKAGE, assistant_review, source_of

SEC = 'sec-xbrl-companyconcept'
TWSE = 'twse-openapi-stock-day-all'
YAHOO = 'yahoo-chart-daily-close'
PACKAGE = Path('tests/fixtures/activation-package-sec-closed.json')
REGISTRY = Path(CLOSED)
ENTITIES = {'schema_version': '1', 'config_version': 'test', 'entities': [
    {'ticker': 'NVDA', 'cik': '0001045810', 'expected_entity_name_contains': 'NVIDIA',
     'exchange': 'NASDAQ', 'currency': 'USD', 'share_class': 'common'},
    {'ticker': 'AMD', 'cik': '0000002488', 'expected_entity_name_contains': 'ADVANCED MICRO DEVICES',
     'exchange': 'NASDAQ', 'currency': 'USD', 'share_class': 'common'}]}


def entities():
    return copy.deepcopy(ENTITIES)


def nvda(config):
    return next(row for row in config['entities'] if row['ticker'] == 'NVDA')


def run(tmp_path, mutate_package=None, config=None, candidate=None, registry=None):
    package = read_json(PACKAGE)
    if mutate_package:
        mutate_package(package)
    path = tmp_path / 'package.json'
    write_json(path, package)
    candidate_path = None
    if candidate is not None:
        candidate_path = tmp_path / 'candidate.json'
        write_json(candidate_path, candidate)
    return check(path, registry or load_registry(REGISTRY), entities() if config is None else config, candidate_path)


def blocked_with(result, fragment):
    assert result['status'] == 'blocked' and result['registry_modified'] is False, result
    assert any(fragment in blocker for blocker in result['blockers']), result['blockers']


def reviewed_candidate():
    """A test-only stand-in for an authorized assistant's registry edit. Never written to config/."""
    payload = read_json(REGISTRY)
    item = read_json(PACKAGE)['items'][0]
    payload['registry_version'] = '0.5.0'
    source = source_of(payload, SEC)
    source.update(declared_status='confirmed', scope=item['proposed_scope'],
                  attribution=item['proposed_attribution'], open_questions=[])
    source['source_review'] = assistant_review(source, extra_urls=[item['dataset_page']])
    return payload


def test_sec_package_is_pending_and_grants_nothing(tmp_path):
    before = REGISTRY.read_bytes()
    result = run(tmp_path)
    assert result['status'] == 'awaiting_source_review', result
    assert result['target'] == 'NVDA' and result['target_kind'] == 'sec_entity'
    assert result['package_drift'] == [] and result['required_review_actions']
    assert result['items'][0]['registry_status'] == 'unknown' and result['registry_modified'] is False
    assert REGISTRY.read_bytes() == before
    assert not load_registry(REGISTRY).snapshot(SEC)['internal_use_allowed']


def test_fixture_matches_the_urls_the_collector_would_request():
    package = read_json(PACKAGE)
    adapter = SecConceptAdapter(SEC, merge_profile(SEC), entities_config=entities())
    assert package['items'][0]['sec_binding']['request_urls'] == [spec.url for spec in adapter.requests('NVDA')]
    assert package['target']['entity_config'] == PROFILES[SEC]['entities_config']


def test_live_sec_entity_config_still_binds_the_fixture_target(capsys):
    """CLI default: a SEC target is resolved against config/sec-entities.json, not the Taiwan file."""
    before = Path('config/source-licenses.json').read_bytes()
    assert main(['activation-check', '--package', str(PACKAGE), '--license-registry', str(REGISTRY)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'awaiting_source_review' and out['target_kind'] == 'sec_entity'
    assert Path('config/source-licenses.json').read_bytes() == before


def test_cli_rejects_another_config_file_for_a_sec_target(tmp_path, capsys):
    other = tmp_path / 'sec-entities.json'
    write_json(other, entities())
    for config in (str(other), CLOSED_INSTRUMENTS):
        assert main(['activation-check', '--package', str(PACKAGE), '--license-registry', str(REGISTRY),
                     '--instruments', config]) == 1
        out = json.loads(capsys.readouterr().out)
        blocked_with(out, '不是審查包指定的')


def set_target(key, value):
    def mutate(package):
        package['target'][key] = value
    return mutate


def set_item(key, value):
    def mutate(package):
        package['items'][0][key] = value
    return mutate


def binding(package):
    return package['items'][0]['sec_binding']


def amd_urls(package):
    binding(package)['request_urls'] = [u.replace('CIK0001045810', 'CIK0000002488') for u in binding(package)['request_urls']]
def drop_url(package): binding(package)['request_urls'].pop()
def reorder_urls(package): binding(package)['request_urls'].reverse()
def other_concept(package): binding(package)['roles']['revenue']['concepts'] = ['Revenues']
def reorder_concepts(package): binding(package)['roles']['eps']['concepts'].reverse()
def other_unit_key(package): binding(package)['roles']['revenue']['units'] = ['EUR']
def other_unit(package): binding(package)['roles']['eps']['unit'] = 'USD'
def other_metric(package): binding(package)['roles']['net_income']['metric_keys'] = ['revenue_current']
def optional_revenue(package): binding(package)['roles']['revenue']['required'] = False
def drop_role(package): del binding(package)['roles']['capital_expenditure']
def extra_role(package):
    binding(package)['roles']['assets'] = {'concepts': ['Assets'], 'units': ['USD'], 'unit': 'USD',
                                           'metric_keys': ['net_income'], 'required': False}
def quarterly_forms(package): binding(package)['annual_forms'] = ['10-K', '10-Q']
def short_period(package): binding(package)['min_period_days'] = 80
def no_binding(package): del package['items'][0]['sec_binding']
def other_field(package): package['items'][0]['field_binding']['entity_name'] = ['name']
def extra_field(package): package['items'][0]['field_binding']['ticker'] = ['ticker']
def other_endpoint(package):
    package['items'][0]['endpoint'] = 'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json'
def other_host(package):
    package['items'][0]['endpoint'] = 'https://example.invalid/api/xbrl/companyconcept/CIK{cik}/us-gaap/{concept}.json'
def twse_item(package): package['items'][0]['source_id'] = TWSE
def yahoo_item(package): package['items'][0]['source_id'] = YAHOO
def unknown_item(package): package['items'][0]['source_id'] = 'sec-unknown-source'
def duplicate_item(package): package['items'].append(copy.deepcopy(package['items'][0]))
def other_baseline(package): package['registry_baseline_version'] = '0.4.0'


@pytest.mark.parametrize('mutate, fragment', [
    (set_target('cik', '0000002488'), 'cik='),
    (set_target('expected_entity_name_contains', 'NVIDIA CORP'), 'expected_entity_name_contains='),
    (set_target('exchange', 'NYSE'), 'exchange='),
    (set_target('share_class', 'class-a'), 'share_class='),
    (set_target('ticker', 'INTC'), '未設定的標的'),
    (set_target('entity_config', 'config/us-price-instruments.json'), 'entity_config'),
    (amd_urls, 'request_urls'), (drop_url, 'request_urls'), (reorder_urls, 'request_urls'),
    (other_concept, 'concept'), (reorder_concepts, 'concept'),
    (other_unit_key, '單位'), (other_unit, '單位'),
    (other_metric, 'metric_keys'), (optional_revenue, 'metric_keys'),
    (drop_role, 'roles'), (extra_role, 'roles'),
    (quarterly_forms, 'annual_forms'), (short_period, 'min_period_days'),
    (no_binding, 'sec_binding'), (other_field, '欄位對應'), (extra_field, '欄位對應'),
    (other_endpoint, '端點'), (other_host, '端點'),
    (twse_item, '不相容'), (yahoo_item, '不相容'), (unknown_item, '不在來源契約'),
    (duplicate_item, '重複'), (other_baseline, '登錄檔版本'),
])
def test_sec_package_mismatch_blocks(tmp_path, mutate, fragment):
    blocked_with(run(tmp_path, mutate), fragment)
    # A candidate is never compared while the package itself has drifted.
    result = run(tmp_path, mutate, candidate=reviewed_candidate())
    assert result['status'] == 'blocked' and 'candidate_changed_sources' not in result


def config_cik(config): nvda(config)['cik'] = '0000002488'
def config_short_cik(config): nvda(config)['cik'] = '1045810'
def config_int_cik(config): nvda(config)['cik'] = 1045810
def config_name(config): nvda(config)['expected_entity_name_contains'] = 'NVIDIA CORP'
def config_no_name(config): del nvda(config)['expected_entity_name_contains']
def config_exchange(config): nvda(config)['exchange'] = 'NYSE'
def config_currency(config): nvda(config)['currency'] = 'TWD'
def config_share_class(config): nvda(config)['share_class'] = 'adr'
def config_removed(config): config['entities'] = [row for row in config['entities'] if row['ticker'] != 'NVDA']
def config_duplicated(config): config['entities'].append(dict(nvda(config), cik='0000002488'))
def config_not_a_list(config): config['entities'] = {'NVDA': nvda(config)}


@pytest.mark.parametrize('mutate, fragment', [
    (config_cik, 'cik='), (config_short_cik, '十位數字'), (config_int_cik, '十位數字'),
    (config_name, 'expected_entity_name_contains='), (config_no_name, '名稱綁定'),
    (config_exchange, 'exchange='), (config_currency, 'currency='), (config_share_class, 'share_class='),
    (config_removed, '未設定的標的'), (config_duplicated, '身份不明確'), (config_not_a_list, 'entities'),
])
def test_sec_entity_config_mismatch_blocks(tmp_path, mutate, fragment):
    config = entities()
    mutate(config)
    blocked_with(run(tmp_path, config=config), fragment)


@pytest.mark.parametrize('config', [
    read_json(CLOSED_INSTRUMENTS), read_json('config/us-price-instruments.json'), {}, {'entities': None}])
def test_wrong_config_kind_blocks_a_sec_target(tmp_path, config):
    blocked_with(run(tmp_path, config=config), 'entities')


@pytest.mark.parametrize('mutate', [
    set_target('cik', '1045810'), set_target('cik', 'CIK0001045810'), set_target('cik', 1045810),
    set_target('currency', 'TWD'), set_target('exchange', 'TWSE'), set_target('expected_entity_name_contains', ' '),
    set_target('code', '2330'), set_target('instrument_config', 'config/tw-instruments.json'),
    lambda p: p['target'].pop('cik'), lambda p: p['target'].pop('expected_entity_name_contains'),
    lambda p: binding(p).update(taxonomy='ifrs-full'), lambda p: binding(p).update(request_urls=[]),
    lambda p: binding(p)['roles']['revenue'].update(concepts=[]),
    lambda p: p['items'][0]['proposed_scope'].update(store_raw_response=True),
])
def test_malformed_or_mixed_sec_target_is_not_a_package(mutate):
    package = read_json(PACKAGE)
    mutate(package)
    with pytest.raises(ValidationError):
        ActivationPackage.model_validate(package)


def test_profile_drift_blocks_the_sec_package(tmp_path, monkeypatch):
    profile = copy.deepcopy(PROFILES[SEC])
    profile['roles']['revenue']['units'] = ['EUR']
    monkeypatch.setitem(PROFILES, SEC, profile)
    result = run(tmp_path)
    blocked_with(result, '單位')
    blocked_with(result, '幣別')
    profile = copy.deepcopy(PROFILES[SEC])
    profile['adapter'] = 'daily_price_series'
    monkeypatch.setitem(PROFILES, SEC, profile)
    blocked_with(run(tmp_path), '不相容')


def test_assistant_reviewed_sec_candidate_matches_only_the_packaged_scope(tmp_path):
    result = run(tmp_path, candidate=reviewed_candidate())
    assert result['status'] == 'candidate_matches_package', result
    assert result['candidate_changed_sources'] == [SEC]
    assert result['candidate_review_basis'] == {SEC: 'assistant_source_review'}

    def source(payload): return source_of(payload, SEC)
    for mutate, fragment in [
        (lambda p: source(p)['scope'].update(store_response_hash=True), None),
        (lambda p: source(p).update(endpoint_scope='https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json'), None),
        (lambda p: source(p).update(attribution='SEC'), '來源標示'),
        (lambda p: source_of(p, YAHOO).update(notes='edited'), '不在審查包建議啟用的範圍'),
        (lambda p: p.update(registry_version='0.4.1'), 'registry_version'),
    ]:
        payload = reviewed_candidate()
        mutate(payload)
        if fragment is None:
            # Either an invalid registry or an out-of-scope change: never a match.
            try:
                outcome = run(tmp_path, candidate=payload)
            except ValidationError:
                continue
            assert outcome['status'] == 'blocked', outcome
        else:
            blocked_with(run(tmp_path, candidate=payload), fragment)


def test_taiwan_package_stays_compatible_and_never_accepts_sec_parts(tmp_path):
    registry, instruments = load_registry(REGISTRY), read_json(CLOSED_INSTRUMENTS)
    result = check(CLOSED_PACKAGE, registry, instruments)
    assert result['status'] == 'awaiting_source_review' and result['target_kind'] == 'tw_instrument'
    # The Taiwan target against the SEC entity map: not configured, blocked.
    assert check(CLOSED_PACKAGE, registry, entities())['status'] == 'blocked'
    path = tmp_path / 'package.json'
    sec_item = read_json(PACKAGE)['items'][0]
    package = read_json(CLOSED_PACKAGE)
    package['items'][0] = sec_item
    write_json(path, package)
    blocked_with(check(path, registry, instruments), '不相容')
    package = read_json(CLOSED_PACKAGE)
    package['items'][0]['sec_binding'] = sec_item['sec_binding']
    write_json(path, package)
    blocked_with(check(path, registry, instruments), '不相容')
    package = read_json(CLOSED_PACKAGE)
    package['items'][0]['source_id'] = YAHOO
    write_json(path, package)
    assert check(path, registry, instruments)['status'] == 'blocked'


def test_cli_default_config_for_a_taiwan_package_is_unchanged(capsys):
    assert main(['activation-check', '--package', CLOSED_PACKAGE, '--license-registry', str(REGISTRY),
                 '--instruments', CLOSED_INSTRUMENTS]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'awaiting_source_review'
    # Without --instruments a Taiwan target still reads config/tw-instruments.json.
    main(['activation-check', '--package', CLOSED_PACKAGE, '--license-registry', str(REGISTRY)])
    out = json.loads(capsys.readouterr().out)
    assert out['target_kind'] == 'tw_instrument' and not any('entities' in b for b in out.get('blockers', []))
