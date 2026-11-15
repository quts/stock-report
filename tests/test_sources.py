"""Offline provider contract tests: field shapes, publication time and failure modes."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pytest
from stock_report.sources import (SourceContractError, build_adapter, merge_profile, parse_source_date,
                                  to_number)
from stock_report.storage import read_json

FIXTURES = Path('tests/fixtures')
COLLECTED_AT = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def payloads(name):
    return read_json(FIXTURES / name)


def twse(overrides=None, data=None, ticker='2330.TW', collected_at=COLLECTED_AT):
    adapter = build_adapter('twse-openapi-stock-day-all', overrides)
    return adapter.parse(ticker, data if data is not None else payloads('twse_stock_day_all.json'), collected_at)


def test_twse_binds_configured_fields_and_keeps_record_identity():
    result = twse()
    assert result.entity.model_dump() == {'entity_id': '2330.TW', 'name': '台積電', 'exchange': 'TWSE',
                                          'currency': 'TWD', 'share_class': 'common'}
    assert result.field_binding['close'] == 'ClosingPrice'
    close = next(o for o in result.observations if o.role == 'close')
    identity = next(o for o in result.observations if o.kind == 'identity')
    assert close.value == 100.0 and close.unit == 'per_share' and close.currency == 'TWD'
    assert close.period == 'close'
    # Provider record identity is kept separate from the normalized metric role.
    assert close.provider_record_id == identity.provider_record_id == '2330@1151113'
    assert close.role == 'close' and identity.role is None
    assert {o.request_key for o in result.observations} == {'daily'}


def test_twse_real_contract_uses_the_roc_trading_date():
    """Confirmed against the recorded smoke sample: STOCK_DAY_ALL carries a ROC `Date`."""
    result = twse()
    close = next(o for o in result.observations if o.role == 'close')
    assert result.field_binding['date'] == 'Date' and result.field_binding['date_format'] == 'roc'
    assert close.source_published_raw is None
    assert close.source_period_raw == '1151113'
    assert close.event_time == datetime(2026, 11, 13, 23, 59, 59, tzinfo=timezone(timedelta(hours=8)))
    for observation in result.observations:
        assert observation.publication_basis == 'observed_availability'
        assert observation.published_at == observation.availability_observed_at == COLLECTED_AT


def test_exchange_quote_without_a_date_column_falls_back_to_observed_availability():
    data = {'daily': [{'Code': '2330', 'Name': '台積電', 'ClosingPrice': '100.00'}]}
    with pytest.raises(SourceContractError, match="no field for 'date'"):
        twse(data=data)
    result = twse(overrides={'required_fields': ['code', 'name', 'close']}, data=data)
    for observation in result.observations:
        assert observation.event_time == COLLECTED_AT and observation.source_published_raw is None
    assert any('觀測可得性上界' in note for note in result.notes)


@pytest.mark.parametrize('raw,expected_variant', [('1151113', 'roc'), ('20261113', 'iso'), ('115/11/13', 'roc')])
def test_declared_calendar_variants_are_explicit(raw, expected_variant):
    rows = [dict(payloads('twse_stock_day_all.json')['daily'][1], Date=raw)]
    result = twse(data={'daily': rows})
    assert result.field_binding['date_format'] == expected_variant
    assert next(o for o in result.observations if o.role == 'close').source_period_raw == raw


def test_undeclared_calendar_variant_is_refused():
    rows = [dict(payloads('twse_stock_day_all.json')['daily'][1], Date='13-Nov-2026')]
    with pytest.raises(SourceContractError, match='matches none of the declared calendar variants'):
        twse(data={'daily': rows})
    with pytest.raises(SourceContractError, match='Undeclared calendar variant'):
        twse(overrides={'date_formats': ['julian']}, data=payloads('twse_stock_day_all.json'))


def test_same_day_session_caps_the_bound_at_the_observation():
    """An afternoon observation of today's session must not be rejected, and must not claim
    an event time later than the moment it was actually observed."""
    rows = [dict(payloads('twse_stock_day_all.json')['daily'][1], Date='1151114')]
    result = twse(data={'daily': rows})
    close = next(o for o in result.observations if o.role == 'close')
    assert close.event_time == COLLECTED_AT
    assert close.source_published_raw is None and close.source_period_raw == '1151114'
    assert close.publication_basis == 'observed_availability'
    assert any('收斂至實際觀測時刻' in note for note in result.notes)


def test_future_session_date_is_still_rejected():
    rows = [dict(payloads('twse_stock_day_all.json')['daily'][1], Date='1151115')]
    with pytest.raises(SourceContractError, match='starts after the observation instant'):
        twse(data={'daily': rows})


TEST_INSTRUMENTS = {'instruments_config': 'tests/fixtures/tw-instruments-test.json'}


def test_twse_missing_close_stays_missing_instead_of_zero():
    result = twse(overrides=TEST_INSTRUMENTS, ticker='2603.TW')
    assert [o.role for o in result.observations] == [None]
    assert any('不以零' in note for note in result.notes)


@pytest.mark.parametrize('ticker', ['0050.TW', '2330', '2330.TWO', '2330A.TW'])
def test_twse_rejects_bad_ticker_shapes(ticker):
    with pytest.raises(ValueError):
        twse(ticker=ticker)


def test_four_digit_code_alone_does_not_establish_a_common_share():
    # Shape passes, but the ticker has no verified instrument-type entry in the project config.
    with pytest.raises(ValueError, match='no verified instrument-type entry'):
        twse(ticker='2603.TW')
    # Configured, but configured as a depositary receipt: refused rather than assumed common.
    with pytest.raises(ValueError, match='depositary_receipt'):
        twse(overrides=TEST_INSTRUMENTS, ticker='9105.TW')


def test_share_class_and_identity_come_from_the_instrument_configuration():
    result = twse()
    assert result.entity.share_class == 'common'
    assert any('股份類別來自' in note and 'tw-instruments.json' in note for note in result.notes)
    rows = [dict(payloads('twse_stock_day_all.json')['daily'][1], Name='其他公司')]
    with pytest.raises(SourceContractError, match='identity check'):
        twse(data={'daily': rows})


def test_twse_unexpected_field_names_fail_with_observed_keys():
    data = {'daily': [{'symbol': '2330', 'name': '台積電', 'close': '100.00', 'date': '1151113'}]}
    with pytest.raises(SourceContractError) as error:
        twse(data=data)
    assert 'observed fields' in str(error.value) and 'symbol' in str(error.value)


def test_twse_profile_override_rebinds_fields_without_code_change():
    data = {'daily': [{'證券代號': '2330', '證券名稱': '台積電', '收盤價': '100.00', '日期': '1151113'}]}
    result = twse(overrides={'fields': {'code': ['證券代號'], 'name': ['證券名稱'], 'close': ['收盤價'],
                                        'date': ['日期']}}, data=data)
    assert next(o for o in result.observations if o.role == 'close').value == 100.0
    assert result.field_binding['code'] == '證券代號'


@pytest.mark.parametrize('data,message', [
    ({'daily': []}, 'empty array'),
    ({'daily': {'Code': '2330'}}, 'expected a JSON array'),
    ({'daily': ['2330']}, 'no JSON objects'),
    ({}, 'expected a JSON array'),
])
def test_twse_malformed_or_empty_responses_fail(data, message):
    with pytest.raises(SourceContractError) as error:
        twse(data=data)
    assert message in str(error.value)


def test_twse_requires_exactly_one_matching_record():
    rows = payloads('twse_stock_day_all.json')['daily']
    with pytest.raises(SourceContractError, match='found 2'):
        twse(data={'daily': rows + [dict(rows[1])]})
    with pytest.raises(SourceContractError, match='found 0'):
        twse(data={'daily': [rows[0]]})


def test_twse_unparseable_number_is_reported_not_guessed():
    rows = [dict(payloads('twse_stock_day_all.json')['daily'][1], ClosingPrice='一百元')]
    with pytest.raises(SourceContractError, match='not parseable'):
        twse(data={'daily': rows})


def tpex(data=None, ticker='6488.TWO', collected_at=COLLECTED_AT):
    adapter = build_adapter('tpex-openapi-mainboard-daily-close')
    return adapter.parse(ticker, data if data is not None else payloads('tpex_mainboard_daily_close.json'), collected_at)


def test_tpex_roc_session_date_becomes_an_event_time_upper_bound():
    result = tpex()
    close = next(o for o in result.observations if o.role == 'close')
    assert close.value == 200.0 and result.entity.exchange == 'TPEx'
    assert close.event_time == datetime(2026, 11, 13, 23, 59, 59, tzinfo=timezone(timedelta(hours=8)))
    assert close.source_published_raw is None
    assert close.source_period_raw == '1151113'
    assert result.field_binding['date_format'] == 'roc'
    # A declared trading date still does not declare when the file was published.
    assert close.publication_basis == 'observed_availability'
    assert close.published_at == COLLECTED_AT


def test_tpex_session_later_than_collection_is_rejected():
    with pytest.raises(SourceContractError, match='starts after the observation instant'):
        tpex(collected_at=datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc))


@pytest.mark.parametrize('raw', ['', 'yesterday', '115/13/45'])
def test_tpex_unparseable_session_date_fails(raw):
    rows = [dict(payloads('tpex_mainboard_daily_close.json')['daily'][1], Date=raw)]
    with pytest.raises((SourceContractError, ValueError)):
        tpex(data={'daily': rows})


def sec(data=None, ticker='NVDA', collected_at=COLLECTED_AT):
    adapter = build_adapter('sec-xbrl-companyconcept')
    return adapter.parse(ticker, data if data is not None else payloads('sec_nvda_concepts.json'), collected_at)


def test_sec_requests_cover_every_configured_concept():
    adapter = build_adapter('sec-xbrl-companyconcept')
    specs = adapter.requests('NVDA')
    assert all('CIK0001045810' in spec.url for spec in specs)
    assert 'revenue:Revenues' in {spec.key for spec in specs}
    with pytest.raises(ValueError, match='not configured'):
        adapter.requests('TSLA')


def test_sec_selects_annual_facts_and_preserves_raw_period_and_unit():
    result = sec()
    roles = {o.role: o for o in result.observations if o.role}
    assert set(roles) == {'revenue_current', 'revenue_prior', 'eps_current', 'eps_prior', 'net_income',
                          'operating_cash_flow', 'capital_expenditure'}
    assert roles['revenue_current'].value == 1234500000 and roles['revenue_current'].period == 'FY2025'
    assert roles['revenue_current'].unit == 'USD' and roles['revenue_current'].source_unit_raw == 'USD'
    assert '2024-01-29..2025-01-26' in roles['revenue_current'].source_period_raw
    assert roles['eps_current'].unit == 'per_share' and roles['eps_current'].source_unit_raw == 'USD/shares'
    # The quarterly fact must never be used as an annual figure.
    assert all(o.value != 300000000 or o.role == 'operating_cash_flow' for o in result.observations)


def test_sec_restated_period_keeps_the_latest_filing_and_its_record_id():
    prior = next(o for o in sec().observations if o.role == 'revenue_prior')
    assert prior.value == 1000000001
    assert prior.provider_record_id.startswith('0001045810-25-000023:')
    assert prior.source_published_raw == '2025-02-26'


def test_sec_revenue_uses_latest_concept_series_without_splicing():
    import copy
    data = payloads('sec_nvda_concepts.json')
    old = data['revenue:RevenueFromContractWithCustomerExcludingAssessedTax']
    newest = copy.deepcopy(old)
    newest['tag'] = 'Revenues'
    newest['units']['USD'].append(dict(start='2025-01-27', end='2026-01-25', val=2000000000,
        accn='0001045810-26-000021', fy=2026, fp='FY', form='10-K', filed='2026-02-25'))
    data['revenue:Revenues'] = newest
    facts = {o.role: o for o in sec(data=data).observations}
    assert facts['revenue_current'].period_end == date(2026, 1, 25)
    assert facts['revenue_prior'].period_end == date(2025, 1, 26)
    assert all(facts[k].request_key == 'revenue:Revenues' for k in ('revenue_current', 'revenue_prior'))
    newest['units']['USD'] = newest['units']['USD'][-1:]
    facts = {o.role: o for o in sec(data=data).observations}
    assert 'revenue_prior' not in facts
    newest['cik'] = 123
    with pytest.raises(SourceContractError, match='does not match configured'):
        sec(data=data)


def test_sec_revenue_same_period_keeps_configured_concept_priority():
    import copy
    data = payloads('sec_nvda_concepts.json')
    data['revenue:Revenues'] = copy.deepcopy(data['revenue:RevenueFromContractWithCustomerExcludingAssessedTax'])
    facts = {o.role: o for o in sec(data=data).observations}
    assert facts['revenue_current'].request_key == 'revenue:RevenueFromContractWithCustomerExcludingAssessedTax'


def test_sec_filing_date_is_a_declared_date_upper_bound():
    current = next(o for o in sec().observations if o.role == 'revenue_current')
    assert current.publication_basis == 'source_declared_date'
    assert current.availability_observed_at is None
    assert current.published_at == datetime(2025, 2, 26, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5)))
    assert current.event_time == datetime(2025, 1, 26, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5)))
    assert current.published_at > current.event_time


def test_sec_identity_comes_from_a_filing_not_from_retrieval_time():
    identity = next(o for o in sec().observations if o.kind == 'identity')
    assert identity.publication_basis == 'source_declared_date'
    assert 'CIK 1045810' in identity.claim
    # The record identifies the field actually observed in the companyconcept response.
    assert identity.provider_record_id.endswith(':response.entityName')
    assert 'entityName' in identity.claim


@pytest.mark.parametrize('field,value,message', [
    ('cik', 1234567, 'does not match configured'),
    ('entityName', 'SOME OTHER ISSUER INC', 'identity check'),
])
def test_sec_identity_mismatch_is_rejected(field, value, message):
    data = payloads('sec_nvda_concepts.json')
    for payload in data.values():
        payload[field] = value
    with pytest.raises(SourceContractError, match=message):
        sec(data=data)


def test_sec_missing_required_role_fails_without_substitution():
    data = {k: v for k, v in payloads('sec_nvda_concepts.json').items() if not k.startswith('revenue:')}
    with pytest.raises(SourceContractError, match='required role revenue'):
        sec(data=data)


def test_sec_optional_role_absence_is_recorded_as_missing():
    data = {k: v for k, v in payloads('sec_nvda_concepts.json').items() if not k.startswith('eps:')}
    result = sec(data=data)
    assert all(o.role not in ('eps_current', 'eps_prior') for o in result.observations)
    assert any('eps' in note for note in result.notes)


def test_sec_quarterly_only_or_wrong_unit_payload_fails():
    data = payloads('sec_nvda_concepts.json')
    key = 'revenue:RevenueFromContractWithCustomerExcludingAssessedTax'
    data[key]['units']['USD'] = [f for f in data[key]['units']['USD'] if f['fp'] != 'FY']
    with pytest.raises(SourceContractError, match='required role revenue'):
        sec(data=data)
    data[key]['units'] = {'EUR': []}
    with pytest.raises(SourceContractError, match='observed units'):
        sec(data=data)


def test_sec_non_adjacent_prior_year_is_left_missing():
    data = payloads('sec_nvda_concepts.json')
    key = 'eps:EarningsPerShareDiluted'
    data[key]['units']['USD/shares'][1].update(start='2021-02-01', end='2022-01-30')
    result = sec(data=data)
    assert all(o.role != 'eps_prior' for o in result.observations)
    assert any('期間起訖不相鄰' in note for note in result.notes)


def test_sec_filing_before_period_end_is_rejected():
    data = payloads('sec_nvda_concepts.json')
    key = 'net_income:NetIncomeLoss'
    data[key]['units']['USD'][0]['filed'] = '2024-12-31'
    with pytest.raises(SourceContractError, match='precedes the period end'):
        sec(data=data)


@pytest.mark.parametrize('payload,message', [
    ({'cik': 1045810, 'entityName': 'NVIDIA CORP'}, 'no units object'),
    ({'cik': 1045810, 'entityName': 'NVIDIA CORP', 'units': {'USD': {}}}, 'does not hold a list'),
])
def test_sec_malformed_concept_payload_fails(payload, message):
    data = payloads('sec_nvda_concepts.json')
    data['revenue:RevenueFromContractWithCustomerExcludingAssessedTax'] = payload
    with pytest.raises(SourceContractError, match=message):
        sec(data=data)


def test_sec_non_object_response_fails():
    data = dict(payloads('sec_nvda_concepts.json'))
    data['revenue:Revenues'] = ['unexpected']
    data.pop('revenue:RevenueFromContractWithCustomerExcludingAssessedTax')
    with pytest.raises(SourceContractError, match='not a JSON object'):
        sec(data=data)


@pytest.mark.parametrize('text,expected', [('1,234.5', 1234.5), ('--', None), ('', None), (None, None), (7, 7.0)])
def test_number_parsing_keeps_blanks_missing(text, expected):
    assert to_number(text) == expected


def test_roc_and_iso_dates():
    assert parse_source_date('1151113', ['roc']) == (date(2026, 11, 13), 'roc')
    assert parse_source_date('115/11/13', ['roc']) == (date(2026, 11, 13), 'roc')
    assert parse_source_date('2026-11-13', ['iso']) == (date(2026, 11, 13), 'iso')
    # A value whose digit count fits a declared variant but is not a real date is refused.
    with pytest.raises(SourceContractError, match='matches none'):
        parse_source_date('132026', ['roc', 'iso'])
    with pytest.raises(SourceContractError, match='matches none'):
        parse_source_date('20261113', ['roc'])


def test_unknown_source_or_profile_key_is_refused():
    with pytest.raises(ValueError, match='Unknown source_id'):
        merge_profile('nasdaq-free-feed')
    with pytest.raises(ValueError, match='Unknown profile key'):
        merge_profile('twse-openapi-stock-day-all', {'secret_backdoor': 1})


def test_sec_adjacent_end_years_with_a_gap_are_not_a_comparable_prior_period():
    """End years 2025 and 2024 look adjacent, but the prior window stops before the current one starts."""
    data = payloads('sec_nvda_concepts.json')
    key = 'revenue:RevenueFromContractWithCustomerExcludingAssessedTax'
    facts = data[key]['units']['USD']
    data[key]['units']['USD'] = [f for f in facts if f['end'] != '2024-01-28'] + [
        {'start': '2023-01-02', 'end': '2024-01-01', 'val': 900000000, 'accn': '0001045810-24-000029',
         'fy': 2024, 'fp': 'FY', 'form': '10-K', 'filed': '2024-02-21'}]
    result = sec(data=data)
    roles = {o.role: o for o in result.observations if o.role}
    assert 'revenue_prior' not in roles
    assert any('revenue 的前期與本期期間起訖不相鄰' in note for note in result.notes)
    # Kept figures carry their original window, not just a fiscal-year label.
    current = roles['revenue_current']
    assert (current.period_start, current.period_end) == (date(2024, 1, 29), date(2025, 1, 26))
    assert current.period_basis == 'fiscal_year' and current.period == 'FY2025'
    assert roles['eps_prior'].period_end + timedelta(days=1) == roles['eps_current'].period_start
