"""Taiwan income-statement adapters: offline contract, fault and quant tests.

Every payload here is an authored shape fixture with illustrative values (see
tests/fixtures/README.md). Field names follow the live contract observed on 2026-11-15;
nothing in this file is a real source value, and no test reaches the network.
"""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from stock_report.cli import main
from stock_report.collect import build_bundle, collect, persist, request_template, write_collection
from stock_report.licensing import load_registry
from stock_report.models import Evidence, Request
from stock_report.pipeline import prepare
from stock_report.quant import calculate
from stock_report.sources import Observation, SourceContractError, build_adapter
from stock_report.storage import read_json

FIXTURES = Path('tests/fixtures')
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
TAIPEI = timezone(timedelta(hours=8))
TWSE_INCOME, TPEX_INCOME = 'twse-openapi-income-general', 'tpex-openapi-income-general'
TWSE_QUOTE = 'twse-openapi-stock-day-all'
ENDPOINTS = {'https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci': ('twse_income_general.json', 'income'),
             'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci': ('tpex_income_general.json', 'income'),
             'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL': ('twse_stock_day_all.json', 'daily')}


@pytest.fixture
def registry():
    return load_registry('config/source-licenses.json')


@pytest.fixture
def attested():
    return load_registry(FIXTURES / 'licenses-attested.json')


class FakeClient:
    """Offline transport keyed by the exact registered endpoint; any other URL is a 'network' failure."""

    def __init__(self, replace=None):
        self.replace, self.calls = replace or {}, []

    def get_json(self, url):
        self.calls.append(url)
        if url in self.replace:
            return self.replace[url], {'attempts': 1, 'cached': False, 'response_sha256': 'c' * 64}
        name, key = ENDPOINTS[url]
        return read_json(FIXTURES / name)[key], {'attempts': 1, 'cached': False, 'response_sha256': 'c' * 64}


def income_rows(name='twse_income_general.json'):
    return read_json(FIXTURES / name)['income']


def parse(source=TWSE_INCOME, ticker='2330.TW', rows=None, overrides=None, collected_at=NOW, **kwargs):
    adapter = build_adapter(source, overrides, **kwargs)
    name = 'twse_income_general.json' if source == TWSE_INCOME else 'tpex_income_general.json'
    return adapter.parse(ticker, {'income': income_rows(name) if rows is None else rows}, collected_at)


def target(rows, code='2330', key='公司代號'):
    return next(r for r in rows if r[key] == code)


def by_role(result):
    return {o.role: o for o in result.observations if o.role}


def collection(source, ticker, registry, **kwargs):
    return collect(source, ticker, registry=registry, allow_network=True, client=FakeClient(), now=NOW, **kwargs)


def tsmc_request(as_of=NOW):
    return Request(mode='stock', ticker='2330.TW', exchange='TWSE', research_as_of=as_of)


# --- response contract ---------------------------------------------------------------------

def test_twse_income_preserves_window_basis_units_and_provenance():
    result = parse()
    assert result.entity.model_dump() == {'entity_id': '2330.TW', 'name': '台積電', 'exchange': 'TWSE',
                                          'currency': 'TWD', 'share_class': 'common'}
    roles = by_role(result)
    assert set(roles) == {'revenue_current', 'net_income', 'eps_current'}
    for observation in roles.values():
        # Original window, basis and raw period identifiers are all kept; nothing is annualized.
        assert (observation.period_start, observation.period_end) == (date(2026, 1, 1), date(2026, 9, 30))
        assert observation.period_basis == 'cumulative_ytd' and observation.period == '2026YTD-Q3'
        assert observation.source_period_raw == '年度=115 季別=3'
        assert observation.provider_record_id == '2330@115Q3@1151114'
        assert observation.event_time == datetime(2026, 9, 30, 23, 59, 59, tzinfo=TAIPEI)
        assert '非單季、非 TTM' in observation.claim
    assert roles['revenue_current'].value == 1000000.0 and roles['net_income'].value == 250000.0
    # The dataset declares no monetary scale, so none is asserted.
    assert roles['revenue_current'].unit == roles['net_income'].unit == 'source_scale_undeclared'
    assert roles['revenue_current'].source_unit_raw is None
    assert roles['eps_current'].value == 2.5 and roles['eps_current'].unit == 'per_share'
    assert roles['eps_current'].source_unit_raw == '元'
    assert result.field_binding == {
        'code': '公司代號', 'name': '公司名稱', 'year': '年度', 'quarter': '季別', 'table_date': '出表日期',
        'revenue': '營業收入', 'net_income_parent': '淨利（淨損）歸屬於母公司業主',
        'eps_basic': '基本每股盈餘（元）', 'table_date_format': 'roc', 'year_calendar': 'roc',
        'period_basis': 'cumulative_ytd'}


def test_table_output_date_is_kept_raw_and_never_becomes_a_publication_time():
    result = parse()
    for observation in result.observations:
        assert observation.publication_basis == 'observed_availability'
        assert observation.published_at == observation.availability_observed_at == NOW
        assert observation.source_published_raw == '出表日期=1151114'
    assert any('出表日期不是公司申報日' in note for note in result.notes)


def test_tpex_income_binds_its_own_header_names():
    result = parse(TPEX_INCOME, '6488.TWO')
    assert result.entity.exchange == 'TPEx' and result.entity.name == '環球晶'
    assert result.field_binding['code'] == 'SecuritiesCompanyCode' and result.field_binding['quarter'] == 'Season'
    roles = by_role(result)
    assert roles['revenue_current'].value == 400000.0 and roles['eps_current'].value == 1.2
    assert roles['net_income'].source_period_raw == 'Year=115 Season=3'
    assert roles['net_income'].source_url == 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci'
    assert roles['net_income'].provider_record_id == '6488@115Q3@1151114'


def test_requests_name_exactly_the_registered_endpoint():
    specs = build_adapter(TWSE_INCOME).requests('2330.TW')
    assert [(s.key, s.url, s.required) for s in specs] == [
        ('income', 'https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci', True)]


def test_notes_state_every_omitted_metric_and_why():
    notes = ' '.join(parse().notes)
    for fragment in ('省略 revenue_growth／eps_growth', '省略 pe', '省略 free_cash_flow', '來源未宣告金額級距',
                     '來源未提供起訖日欄位'):
        assert fragment in notes


@pytest.mark.parametrize('quarter,end', [('1', date(2026, 3, 31)), ('2', date(2026, 6, 30)),
                                         ('4', date(2026, 12, 31))])
def test_window_follows_the_declared_quarter_and_stays_cumulative(quarter, end):
    rows = income_rows()
    target(rows).update({'季別': quarter, '出表日期': '1160320'})
    result = parse(rows=rows, collected_at=datetime(2027, 3, 21, tzinfo=timezone.utc))
    revenue = by_role(result)['revenue_current']
    assert (revenue.period_start, revenue.period_end) == (date(2026, 1, 1), end)
    # A full-year cumulative figure is still not relabelled as an annual or trailing figure.
    assert revenue.period_basis == 'cumulative_ytd' and revenue.period == f'2026YTD-Q{quarter}'


def test_losses_keep_their_sign():
    config = {'config_version': 't', 'instruments': [{
        'ticker': '1101.TW', 'code': '1101', 'exchange': 'TWSE', 'instrument_type': 'common_share',
        'share_class': 'common', 'verified_by': 'test-fixture', 'financial_statement_format': 'general',
        'fiscal_year_end_month': 12}]}
    roles = by_role(parse(ticker='1101.TW', instruments_config=config))
    assert roles['net_income'].value == -20000.0 and roles['eps_current'].value == -0.4


# --- response faults -----------------------------------------------------------------------

def test_blank_value_stays_missing_and_is_not_replaced_by_another_line():
    rows = income_rows()
    target(rows)['淨利（淨損）歸屬於母公司業主'] = ''
    result = parse(rows=rows)
    assert 'net_income' not in by_role(result)
    assert any(note.startswith('net_income：來源欄位為空') for note in result.notes)


def test_renamed_or_missing_field_fails_with_observed_keys():
    rows = [{('營收' if k == '營業收入' else k): v for k, v in row.items()} for row in income_rows()]
    with pytest.raises(SourceContractError, match="no field for 'revenue'.*observed fields"):
        parse(rows=rows)


def test_profile_override_rebinds_a_field_without_code_change():
    rows = [{('營收' if k == '營業收入' else k): v for k, v in row.items()} for row in income_rows()]
    result = parse(rows=rows, overrides={'fields': {'revenue': ['營收']}})
    assert result.field_binding['revenue'] == '營收' and by_role(result)['revenue_current'].value == 1000000.0


def test_eps_field_without_a_declared_unit_is_not_assumed_per_share():
    rows = [{('基本每股盈餘' if k == '基本每股盈餘（元）' else k): v for k, v in row.items()} for row in income_rows()]
    with pytest.raises(SourceContractError, match='does not declare the expected unit'):
        parse(rows=rows, overrides={'fields': {'eps_basic': ['基本每股盈餘']}})


@pytest.mark.parametrize('rows,message', [
    ({'unexpected': 'object'}, 'expected a JSON array'),
    ([], 'empty array'),
    (['text'], 'no JSON objects'),
])
def test_malformed_or_empty_tables_fail_without_substitution(rows, message):
    with pytest.raises(SourceContractError, match=message):
        parse(rows=rows)


def test_exactly_one_issuer_row_is_required():
    rows = income_rows()
    with pytest.raises(SourceContractError, match='exactly one record for 2330, found 2'):
        parse(rows=rows + [dict(target(rows))])
    with pytest.raises(SourceContractError, match='exactly one record for 2330, found 0'):
        parse(rows=[r for r in rows if r['公司代號'] != '2330'])


def test_issuer_name_must_pass_the_configured_identity_check():
    rows = income_rows()
    target(rows)['公司名稱'] = '另一家公司'
    with pytest.raises(SourceContractError, match='does not contain the configured identity check'):
        parse(rows=rows)


@pytest.mark.parametrize('field,value,message', [
    ('年度', '2026', 'is not a ROC year'),
    ('年度', '', 'is not a ROC year'),
    ('年度', '一一五', 'is not a ROC year'),
    ('季別', '5', 'is not one of 1-4'),
    ('季別', '0', 'is not one of 1-4'),
    ('季別', 'Q3', 'is not one of 1-4'),
    ('季別', '', 'is not one of 1-4'),
    ('出表日期', '', 'empty table output date'),
    ('出表日期', '14-Nov-2026', 'matches none of the declared calendar variants'),
    ('出表日期', '20261114', 'matches none of the declared calendar variants'),
    ('出表日期', '1150930', 'is not after the period end'),
    ('出表日期', '1150101', 'is not after the period end'),
    ('出表日期', '1151120', 'starts after the observation instant'),
    ('營業收入', '1,0x0', 'Numeric field is not parseable'),
])
def test_period_date_and_numeric_faults_are_refused(field, value, message):
    rows = income_rows()
    target(rows)[field] = value
    with pytest.raises(SourceContractError, match=message):
        parse(rows=rows)


def test_a_period_that_has_not_ended_at_observation_is_refused():
    with pytest.raises(SourceContractError, match='had not ended at the observation instant'):
        parse(collected_at=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc))


def test_unverified_period_basis_cannot_be_switched_on_by_override():
    with pytest.raises(SourceContractError, match='has no verified contract'):
        parse(overrides={'period_basis': 'single_quarter'})
    with pytest.raises(SourceContractError, match='undeclared year calendar'):
        parse(overrides={'year_calendar': 'gregorian'})


# --- issuer, market and instrument gate ----------------------------------------------------

def instruments(**change):
    row = {'ticker': '2330.TW', 'code': '2330', 'exchange': 'TWSE', 'instrument_type': 'common_share',
           'share_class': 'common', 'expected_name_contains': '台積電', 'verified_by': 'test-fixture',
           'financial_statement_format': 'general', 'fiscal_year_end_month': 12}
    row.update(change)
    return {'config_version': 't', 'instruments': [{k: v for k, v in row.items() if v is not None}]}


@pytest.mark.parametrize('change,message', [
    ({'fiscal_year_end_month': None}, 'no supported fiscal_year_end_month'),
    ({'fiscal_year_end_month': 6}, 'no supported fiscal_year_end_month'),
    ({'financial_statement_format': None}, 'financial_statement_format'),
    ({'financial_statement_format': 'financial_holding'}, 'financial_statement_format'),
    ({'instrument_type': 'depositary_receipt'}, 'does not support'),
    ({'exchange': 'TPEx'}, 'does not match this source market'),
    ({'code': '2331'}, 'does not match this source market or code'),
])
def test_instrument_configuration_gates_requests_and_parsing(change, message):
    adapter = build_adapter(TWSE_INCOME, instruments_config=instruments(**change))
    with pytest.raises(ValueError, match=message):
        adapter.requests('2330.TW')
    with pytest.raises(ValueError, match=message):
        adapter.parse('2330.TW', {'income': income_rows()}, NOW)


@pytest.mark.parametrize('source,ticker,message', [
    (TWSE_INCOME, '6488.TWO', 'serves TWSE tickers'),
    (TPEX_INCOME, '2330.TW', 'serves TPEx tickers'),
    (TWSE_INCOME, '2603.TW', 'no verified instrument-type entry'),
    (TWSE_INCOME, '00878.TW', 'documented code shape'),
])
def test_wrong_market_or_unconfigured_issuer_is_refused(source, ticker, message):
    with pytest.raises(ValueError, match=message):
        build_adapter(source).requests(ticker)


def test_project_configuration_declares_both_targets_explicitly():
    rows = {r['ticker']: r for r in read_json('config/tw-instruments.json')['instruments']}
    for ticker in ('2330.TW', '6488.TWO'):
        assert rows[ticker]['financial_statement_format'] == 'general'
        assert rows[ticker]['fiscal_year_end_month'] == 12
        assert rows[ticker]['verified_by'] == 'assistant-poc-declaration'
        assert '不得視為人工簽認' in rows[ticker]['financial_basis_evidence']


# --- period model --------------------------------------------------------------------------

def test_a_period_window_needs_both_ends_and_a_basis():
    base = parse().observations[1].model_dump()
    for change in ({'period_end': None}, {'period_basis': None},
                   {'period_start': date(2026, 10, 1)}):
        with pytest.raises(ValidationError):
            Observation.model_validate({**base, **change})


# --- collection, licence and persistence ---------------------------------------------------

def test_default_registry_keeps_both_income_sources_unknown(registry):
    for source in (TWSE_INCOME, TPEX_INCOME):
        snapshot = registry.snapshot(source)
        assert snapshot['status'] == 'unknown' and not snapshot['human_attested'] and snapshot['attested_by'] is None
        assert not any(snapshot[name] for name in ('internal_use_allowed', 'pdf_share_allowed',
                                                   'store_response_hash', 'store_raw_response'))
        assert all(row['checked_by'] == 'assistant' for row in snapshot['terms_evidence'])
        assert len(snapshot['open_questions']) >= 4


def test_unknown_licence_persists_contract_metadata_only(registry, tmp_path):
    captured = collection(TWSE_INCOME, '2330.TW', registry)
    assert captured.capture_mode == 'network' and captured.retention == 'feasibility_only'
    assert captured.coverage_mode == 'latest_only'
    assert all('response_sha256' not in row for row in captured.transport)
    with pytest.raises(ValueError, match='不得寫入任何來源數值'):
        write_collection(captured, tmp_path)
    path, artifact = persist(captured, tmp_path)
    assert artifact == 'inspection_record' and not list(tmp_path.rglob('collection.json'))
    record, text = read_json(path), path.read_text()
    assert record['blockers'][0]['code'] == 'source_values_not_cleared_for_local_persistence'
    assert record['field_binding']['period_basis'] == 'cumulative_ytd'
    financial = [o for o in record['observations'] if o['kind'] == 'financial']
    assert [o['role'] for o in financial] == ['revenue_current', 'net_income', 'eps_current']
    assert all(o['period_basis'] == 'cumulative_ytd' and o['period_window_present'] and o['value_present']
               for o in financial)
    assert [o['source_unit_declared'] for o in financial] == [False, False, True]
    # No value, issuer name, record identifier or period stamp of the source is retained.
    for secret in ('1000000', '250000', '2.5', '台積電', '2330@', '1151114', '2026-09-30'):
        assert secret not in text


def test_unknown_licence_never_reaches_a_bundle(registry):
    data, report = build_bundle(tsmc_request(), [collection(TWSE_INCOME, '2330.TW', registry)], registry=registry)
    assert data is None and report['status'] == 'blocked'
    assert {row['reason'] for row in report['exclusions']} == {'license_not_confirmed_for_internal_use'}


def test_endpoint_outside_the_registered_dataset_is_refused(attested):
    other = {'endpoint': 'https://openapi.twse.com.tw/v1/opendata/t187ap06_L_fh'}
    with pytest.raises(ValueError, match='outside the reviewed scope'):
        collect(TWSE_INCOME, '2330.TW', registry=attested, allow_network=True, client=FakeClient(), now=NOW,
                profile_overrides=other)


def test_no_network_opt_in_and_transport_failure_never_fall_back(registry):
    with pytest.raises(ValueError, match='requires --allow-network'):
        collect(TWSE_INCOME, '2330.TW', registry=registry)

    class Down:
        def get_json(self, url):
            raise TimeoutError('timed out')
    with pytest.raises(TimeoutError):
        collect(TWSE_INCOME, '2330.TW', registry=registry, allow_network=True, client=Down(), now=NOW)


def test_fixture_capture_is_never_promoted_to_real(attested):
    captured = collect(TWSE_INCOME, '2330.TW', registry=attested, fixture=FIXTURES / 'twse_income_general.json',
                       now=NOW)
    assert captured.capture_mode == 'fixture'
    data, report = build_bundle(tsmc_request(), [captured], registry=attested)
    assert data is None and report['blockers'][0]['code'] == 'fixture_capture_not_real_evidence'
    data, report = build_bundle(tsmc_request(), [captured], registry=attested, allow_fixture_capture=True)
    assert data.dataset_kind == 'mock' and all(e.source_type == 'synthetic' for e in data.evidence)


# --- bundle, cutoff and deterministic quant ------------------------------------------------

def test_attested_income_and_quote_build_a_bundle_with_only_verifiable_metrics(attested, tmp_path):
    income = collection(TWSE_INCOME, '2330.TW', attested)
    quote = collection(TWSE_QUOTE, '2330.TW', attested)
    request = tsmc_request()
    data, report = build_bundle(request, [income, quote], registry=attested)
    assert report['status'] == 'ready' and data.dataset_kind == 'real'
    assert report['core_source_records'] == 2 and not report['coverage_warnings']
    evidence = {e.metric_key: e for e in data.evidence if e.metric_key}
    assert evidence['revenue_current'].period_start == date(2026, 1, 1)
    assert evidence['revenue_current'].period_end == date(2026, 9, 30)
    assert evidence['revenue_current'].period_basis == 'cumulative_ytd'
    assert evidence['eps_current'].source_unit_raw == '元'
    assert evidence['eps_current'].source_period_raw == '年度=115 季別=3'
    metrics = {m.name: m for m in data.metrics}
    # Same record, same window, same undeclared scale: the ratio is the only derivable figure.
    assert metrics['net_margin'].value == pytest.approx(25.0) and metrics['net_margin'].period == '2026YTD-Q3'
    assert metrics['net_margin'].unit == 'percent'
    for name in ('revenue_growth', 'eps_growth', 'free_cash_flow', 'pe'):
        assert metrics[name].value is None and metrics[name].missing_reason
    assert 'revenue_prior' in metrics['revenue_growth'].missing_reason
    assert '年初至今累計數，不年化' in metrics['revenue_growth'].missing_reason
    assert 'operating_cash_flow' in metrics['free_cash_flow'].missing_reason
    # Close and cumulative EPS are both present, and a PE is still refused rather than annualized.
    assert evidence['close'].value == 100.0 and evidence['eps_current'].value == 2.5
    assert '2026YTD-Q3' in metrics['pe'].missing_reason and 'cumulative_ytd' in metrics['pe'].missing_reason
    assert '年化' in metrics['pe'].missing_reason
    result = prepare(request, tmp_path, data.model_dump(mode='json'))
    assert result['coverage']['2330.TW'] == {'identity': 'available', 'financial': 'available',
                                             'price': 'available', 'event': 'unavailable',
                                             'rating': 'unavailable', 'relationship': 'unavailable'}
    stored = read_json(Path(result['research_input_path']))
    assert stored['dataset_kind'] == 'real'
    assert {m['name']: m['value'] for m in stored['metrics']}['pe'] is None


def test_tpex_income_bundle_keeps_issuer_and_market(attested):
    captured = collection(TPEX_INCOME, '6488.TWO', attested)
    request = Request(mode='stock', ticker='6488.TWO', exchange='TPEx', research_as_of=NOW)
    data, report = build_bundle(request, [captured], registry=attested)
    assert report['status'] == 'ready' and data.entities[0].exchange == 'TPEx'
    assert {m.name: m.value for m in data.metrics}['net_margin'] == pytest.approx(15.0)
    # One filing record is below the direction threshold: reported, not worked around.
    assert report['coverage_warnings'][0]['code'] == 'core_evidence_below_direction_threshold'
    wrong_market = Request(mode='stock', ticker='6488.TWO', exchange='TWSE', research_as_of=NOW)
    data, report = build_bundle(wrong_market, [captured], registry=attested)
    assert data is None and 'request_exchange_mismatch' in {b['code'] for b in report['blockers']}
    data, report = build_bundle(tsmc_request(), [captured], registry=attested)
    assert data is None and 'collection_ticker_mismatch' in {b['code'] for b in report['blockers']}


def test_historical_cutoff_before_observation_excludes_the_statement(attested):
    captured = collection(TWSE_INCOME, '2330.TW', attested)
    # After the table output date's start but before the observation: still not supported.
    request = tsmc_request(NOW - timedelta(hours=1))
    data, report = build_bundle(request, [captured], registry=attested)
    assert data is None and request.research_as_of == NOW - timedelta(hours=1)
    assert {row['reason'] for row in report['exclusions']} == {
        'historical_as_of_not_supported_by_observed_availability'}
    with pytest.raises(ValueError, match='cannot be supported'):
        request_template('stock', '2330.TW', 'TWSE', [captured], NOW - timedelta(days=30))


def financial(data, key):
    return next(e for e in data.evidence if e.source_record_id == key)


def metric(data, name):
    return {m.name: m for m in calculate(data.evidence, data.request.research_as_of)}[name]


def set_window(evidence, start, end, basis='fiscal_year'):
    evidence.period_start, evidence.period_end, evidence.period_basis = start, end, basis


def test_equal_labels_do_not_equate_different_windows(data):
    """Both figures say FY2025, but their windows differ: the margin is refused."""
    set_window(financial(data, 'revenue_current'), date(2025, 1, 1), date(2025, 12, 31))
    set_window(financial(data, 'net_income'), date(2024, 7, 1), date(2025, 6, 30))
    result = metric(data, 'net_margin')
    assert result.value is None and '期間起訖不同' in result.missing_reason
    set_window(financial(data, 'net_income'), date(2025, 1, 1), date(2025, 12, 31))
    assert metric(data, 'net_margin').value == 20


def test_adjacent_year_labels_do_not_make_windows_comparable(data):
    """FY2025 vs FY2024 labels, but the prior window does not end where the current one starts."""
    set_window(financial(data, 'revenue_current'), date(2025, 1, 1), date(2025, 12, 31))
    set_window(financial(data, 'revenue_prior'), date(2023, 7, 1), date(2024, 6, 30))
    result = metric(data, 'revenue_growth')
    assert result.value is None and '不相鄰' in result.missing_reason
    set_window(financial(data, 'revenue_prior'), date(2024, 1, 1), date(2024, 12, 31))
    assert metric(data, 'revenue_growth').value == 20


def test_basis_mismatch_one_sided_and_official_windowless_figures_are_refused(data):
    current, prior = financial(data, 'revenue_current'), financial(data, 'revenue_prior')
    set_window(current, date(2025, 1, 1), date(2025, 12, 31))
    assert '僅一方記錄期間起訖' in metric(data, 'revenue_growth').missing_reason
    set_window(prior, date(2024, 1, 1), date(2024, 12, 31), 'cumulative_ytd')
    assert '期間基礎不同' in metric(data, 'revenue_growth').missing_reason
    set_window(current, date(2025, 1, 1), date(2025, 12, 31), 'cumulative_ytd')
    assert '不計算成長率' in metric(data, 'revenue_growth').missing_reason
    set_window(current, None, None, None)
    set_window(prior, None, None, None)
    assert metric(data, 'revenue_growth').value == 20
    current.source_type = 'official'
    assert '官方財務證據缺少期間起訖' in metric(data, 'revenue_growth').missing_reason


def test_official_or_non_trailing_eps_never_yields_a_pe(data):
    eps = financial(data, 'eps_current')
    assert metric(data, 'pe').value == 25
    set_window(eps, date(2026, 1, 1), date(2026, 9, 30), 'cumulative_ytd')
    # Even mislabelled as TTM, a cumulative window is not a trailing twelve months.
    assert metric(data, 'pe').value is None and '年化' in metric(data, 'pe').missing_reason
    set_window(eps, None, None, None)
    eps.source_type = 'official'
    assert metric(data, 'pe').value is None


def test_evidence_model_rejects_an_incomplete_window(data):
    payload = financial(data, 'revenue_current').model_dump(mode='json')
    with pytest.raises(ValidationError):
        Evidence.model_validate({**payload, 'period_start': '2025-01-01'})
    with pytest.raises(ValidationError):
        Evidence.model_validate({**payload, 'period_start': '2025-01-01', 'period_end': '2025-12-31'})


# --- CLI integration -----------------------------------------------------------------------

def test_cli_fixture_flow_stays_mock_and_is_refused_by_import(tmp_path, capsys):
    collections = tmp_path / 'collections'
    licences = str(FIXTURES / 'licenses-attested.json')
    assert main(['collect', '--source', TPEX_INCOME, '--ticker', '6488.TWO', '--fixture',
                 str(FIXTURES / 'tpex_income_general.json'), '--output', str(collections),
                 '--license-registry', licences]) == 0
    collected = json.loads(capsys.readouterr().out)
    assert collected['capture_mode'] == 'fixture' and collected['coverage_mode'] == 'latest_only'
    assert collected['field_binding']['period_basis'] == 'cumulative_ytd'
    run = Path(collected['collection_path']).parent
    request, bundle = tmp_path / 'request.json', tmp_path / 'bundle.json'
    assert main(['request-template', '--ticker', '6488.TWO', '--exchange', 'TPEx', '--collection', str(run),
                 '--output', str(request)]) == 0
    capsys.readouterr()
    assert main(['bundle', '--request', str(request), '--collection', str(run), '--output', str(bundle),
                 '--license-registry', licences]) == 1
    assert 'fixture_capture_not_real_evidence' in {b['code'] for b in json.loads(capsys.readouterr().out)['blockers']}
    assert main(['bundle', '--request', str(request), '--collection', str(run), '--output', str(bundle),
                 '--license-registry', licences, '--allow-fixture-capture']) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready['dataset_kind'] == 'mock'
    report = read_json(tmp_path / 'bundle-report.json')
    assert {m['name']: m['value'] for m in report['metrics']}['net_margin'] == pytest.approx(15.0)
    assert main(['prepare', '--request', str(request), '--provider', 'import', '--evidence-bundle', str(bundle),
                 '--output', str(tmp_path / 'runs')]) == 1
    assert 'real evidence bundles' in json.loads(capsys.readouterr().out)['message']


def test_cli_network_capture_under_unknown_licence_is_inspection_only(tmp_path, capsys, monkeypatch, registry):
    import stock_report.cli as cli_module
    captured = collection(TWSE_INCOME, '2330.TW', registry)
    monkeypatch.setattr(cli_module, 'collect', lambda *args, **kwargs: captured)
    assert main(['collect', '--source', TWSE_INCOME, '--ticker', '2330.TW', '--allow-network',
                 '--output', str(tmp_path)]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result['status'] == 'inspection_only' and result['persisted'] == 'inspection_record'
    assert '台積電' not in output and '1000000' not in output
    assert not list(tmp_path.rglob('collection.json'))


def test_undeclared_scale_is_not_shared_across_two_retrievals_of_the_same_source(attested):
    """Revenue from one capture and net income from a later one are not assumed to share a scale."""
    first = collection(TWSE_INCOME, '2330.TW', attested)
    data, _ = build_bundle(tsmc_request(), [first], registry=attested)
    assert metric(data, 'net_margin').value == pytest.approx(25.0)
    next(e for e in data.evidence if e.metric_key == 'net_income').retrieved_at -= timedelta(minutes=5)
    result = metric(data, 'net_margin')
    assert result.value is None and '不假設級距相同' in result.missing_reason
