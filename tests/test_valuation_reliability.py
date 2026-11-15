"""Market data, driver-based forecasts, multiple references and analytical quality gates."""
from datetime import date, datetime, timedelta, timezone
import csv
import json
import pytest
from stock_report import engine, market_data
from stock_report.calendars import is_trading_day, nyse_holidays, trading_days_after
from stock_report.models import Assumption, ScenarioProposal
from stock_report.operator_import import evidence as imported_evidence, read_csv
from engine_fixtures import DELL_QUARTERS, dell, ev, fin
from test_investment_engine import DELL_HORIZON, default_scenarios, driver, gate, proposal_for, scenario

UTC = timezone.utc
ATTEST = {'attested_by': 'Test Operator', 'attested_at': '2026-11-15T00:00:00+00:00',
          'rights_statement': 'I licensed this data for my own private research use (test fixture).',
          'source_name': 'Test data vendor', 'source_url': 'https://vendor.example/terms'}


def import_file(kind, rows, ticker='DELL', exchange='NYSE', attestation=ATTEST):
    return {'kind': kind, 'ticker': ticker, 'exchange': exchange, 'attestation': attestation, 'rows': rows}


def with_imports(data, *files):
    data.evidence = data.evidence + [e for f in files for e in imported_evidence(f, data.request)]
    return data


def quarter_end_prices(pe=12.0):
    """Close at each DELL quarter end implying a constant trailing P/E (test-only)."""
    data = dell()
    eps = {e.period_end: e.value for e in data.evidence if e.financial_metric == 'diluted_eps'}
    ends = [end for _, end in DELL_QUARTERS]
    rows = []
    for i in range(3, len(ends)):
        ttm = sum(eps[e] for e in ends[i - 3:i + 1])
        rows.append({'date': ends[i].isoformat(), 'close': round(pe * ttm, 2), 'currency': 'USD', 'adjustment': 'unadjusted'})
    return rows


# --- calendars ------------------------------------------------------------------------------

def test_nyse_holidays_follow_exchange_rules():
    h = nyse_holidays(2026)
    assert date(2026, 4, 3) in h            # Good Friday
    assert date(2026, 11, 26) in h          # Thanksgiving
    assert date(2026, 7, 3) in h            # Independence Day observed (Jul 4 is Saturday)
    assert date(2027, 12, 31) not in nyse_holidays(2027)   # Saturday New Year 2028 is not observed early
    assert trading_days_after('NYSE', date(2026, 11, 25), date(2026, 11, 30)) == (2, True)


def test_twse_calendar_uses_official_list_and_flags_unverified_years():
    assert is_trading_day('TWSE', date(2027, 2, 9)) == (False, True)     # Lunar New Year
    assert is_trading_day('TWSE', date(2027, 2, 11)) == (True, True)
    assert is_trading_day('TWSE', date(2026, 11, 13)) == (True, False)   # 2026 list not published any more


# --- price retrieval, import and failure handling -----------------------------------------------

def test_missing_price_keeps_financial_analysis_but_blocks_valuation_and_explains_providers():
    data = dell(price=None)
    result = engine.run(data, proposal_for(data))
    assert any(m['status'] == 'available' for m in result['analysis']['metrics'])
    assert result['valuation']['assessment'] == 'unavailable'
    providers = {p['provider']: p['status'] for p in result['market_data']['diagnostics']['providers']}
    assert providers['operator-import'] == 'operator_attested_import'
    assert providers['yahoo-chart-daily-close'].startswith('closed')     # unsupported licensing never used
    assert 'import-market-data' in result['market_data']['diagnostics']['action']
    assert all(s['change_vs_reference_percent'] is None for s in result['scenarios']['scenarios'])
    assert 'price_recency' in result['gates']['critical_failures']


def test_operator_import_supplies_a_us_close_with_provenance():
    data = with_imports(dell(price=None), import_file('prices', [
        {'date': '2026-11-13', 'close': 150.0, 'currency': 'USD', 'adjustment': 'unadjusted'}]))
    result = engine.run(data)
    price = result['valuation']['market']['price']
    assert price['status'] == 'available' and price['provider'] == 'operator-import' and price['value'] == 150.0
    close = next(e for e in data.evidence if e.evidence_id.startswith('import-DELL-close'))
    assert close.source_type == 'secondary' and 'Test Operator' in close.attribution


def test_import_rejects_ai_attestation_zero_price_wrong_currency_and_holidays(tmp_path):
    path = tmp_path / 'p.csv'
    def write(rows):
        with open(path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['date', 'close', 'currency'])
            w.writeheader(); w.writerows(rows)
    now = datetime(2026, 11, 16, tzinfo=UTC)
    write([{'date': '2026-11-13', 'close': '150', 'currency': 'USD'}])
    with pytest.raises(ValueError, match='person'):
        read_csv('prices', path, 'DELL', 'NYSE', {**ATTEST, 'attested_by': 'Claude Code'}, now)
    for row, match in (({'date': '2026-11-13', 'close': '0', 'currency': 'USD'}, 'positive'),
                       ({'date': '2026-11-13', 'close': '150', 'currency': 'TWD'}, 'currency'),
                       ({'date': '2026-11-26', 'close': '150', 'currency': 'USD'}, 'trading day')):
        write([row])
        with pytest.raises(ValueError, match=match):
            read_csv('prices', path, 'DELL', 'NYSE', ATTEST, now)
    write([{'date': '2026-11-13', 'close': '150', 'currency': 'USD'}] * 2)
    with pytest.raises(ValueError, match='duplicate'):
        read_csv('prices', path, 'DELL', 'NYSE', ATTEST, now)


def test_prices_after_cutoff_are_not_imported():
    data = dell(price=None)
    rows = imported_evidence(import_file('prices', [{'date': '2026-11-20', 'close': 1.0, 'currency': 'USD',
                                                      'adjustment': 'unadjusted'}]), data.request)
    assert rows == []


def test_conflicting_and_invalid_prices_are_excluded_not_averaged():
    data = dell()
    other = ev('dell-close-other', 'DELL', data.request.research_as_of, kind='price', metric_key='close', value=170.0,
               unit='per_share', currency='USD', period_start=date(2026, 11, 13), period_end=date(2026, 11, 13),
               period_basis='trading_day', event_time=next(e for e in data.evidence if e.evidence_id == 'dell-close').event_time,
               license_source_id='operator-import')
    data.evidence.append(other)
    result = engine.run(data)
    assert result['market_data']['diagnostics']['conflicts'][0]['date'] == '2026-11-13'
    assert result['valuation']['market']['price']['status'] == 'conflicting'


def test_stale_price_over_a_holiday_is_judged_with_the_calendar():
    # Close on Wed 2026-11-25; Thanksgiving is closed, so by Mon 11-30 only two sessions have passed.
    fresh = engine.run(dell(cutoff=datetime(2026, 11, 30, 23, tzinfo=UTC), price_day=date(2026, 11, 25)))
    assert fresh['valuation']['market']['price']['status'] == 'available'
    stale = engine.run(dell(price_day=date(2026, 11, 6)))
    assert stale['valuation']['market']['price']['status'] == 'stale'


def test_split_adjusts_history_but_not_the_latest_close():
    data = with_imports(dell(), import_file('splits', [{'date': '2026-09-01', 'ratio': 2.0}]))
    series = market_data.prices(data, data.entities[0])
    assert series['latest']['adjusted_close'] == series['latest']['close']
    assert series['diagnostics']['splits'][0]['ratio'] == 2.0


# --- historical and peer references ----------------------------------------------------------------

def test_historical_price_gaps_leave_the_range_limited():
    data = with_imports(dell(price=None), import_file('prices', quarter_end_prices()[-3:] +
                                                       [{'date': '2026-11-13', 'close': 150.0, 'currency': 'USD', 'adjustment': 'unadjusted'}]))
    result = engine.run(data)
    ref = result['valuation']['references']['historical_pe']
    assert ref['status'] == 'limited' and ref['stats']['count'] == 3
    assert result['valuation']['assessment'] == 'current_multiples_only'


def test_sufficient_history_makes_a_multiple_evidence_based():
    rows = quarter_end_prices(12.0) + [{'date': '2026-11-13', 'close': 150.0, 'currency': 'USD', 'adjustment': 'unadjusted'}]
    data = with_imports(dell(price=None), import_file('prices', rows))
    scen = [scenario('bear', 0, 12.0, margin=7.0, margin_basis='model_assumption', mbasis='historical_range'),
            scenario('base', 15, 12.0, mbasis='historical_range'),
            scenario('bull', 25, 12.0, margin=8.0, margin_basis='model_assumption', mbasis='historical_range')]
    result = engine.run(data, proposal_for(data, scen))
    ref = result['valuation']['references']['historical_pe']
    assert ref['status'] == 'sufficient' and ref['stats']['median'] == pytest.approx(12.0, rel=0.01)
    assert result['scenarios']['valuation_basis'] == 'evidence_based'
    assert gate(result, 'valuation_reference_quality')['status'] == 'PASS'
    assert result['valuation']['assessment'] == 'evaluated'


def test_multiple_outside_the_historical_range_fails():
    rows = quarter_end_prices(12.0) + [{'date': '2026-11-13', 'close': 150.0, 'currency': 'USD', 'adjustment': 'unadjusted'}]
    data = with_imports(dell(price=None), import_file('prices', rows))
    scen = [scenario('bear', 0, 10, mbasis='historical_range'), scenario('base', 15, 12.0, mbasis='historical_range'),
            scenario('bull', 25, 30, mbasis='historical_range')]
    result = engine.run(data, proposal_for(data, scen))
    assert gate(result, 'valuation_reference_quality')['status'] == 'FAIL'


def test_peer_multiples_need_three_peers():
    two = [{'peer': p, 'metric': 'pe_trailing', 'value': v, 'as_of': '2026-11-13'} for p, v in (('HPE', 11), ('SMCI', 14))]
    data = with_imports(dell(), import_file('peers', two))
    assert engine.run(data)['valuation']['references']['peers']['pe_trailing']['status'] == 'limited'
    three = two + [{'peer': 'HPQ', 'metric': 'pe_trailing', 'value': 9, 'as_of': '2026-11-13'}]
    data = with_imports(dell(), import_file('peers', three))
    result = engine.run(data)
    peer = next(r for r in result['valuation']['results'] if r['method'] == 'peer_relative')
    assert peer['status'] == 'available' and peer['value'] == 11


def test_imported_consensus_is_never_compared_across_gaap_and_non_gaap():
    rows = [{'provider': 'Test Consensus', 'metric': 'diluted_eps', 'period_end': '2026-10-30', 'period_label': 'FY2027 Q3',
             'accounting_basis': 'non_gaap', 'value': 6.6, 'unit': 'per_share', 'currency': 'USD',
             'estimate_date': '2026-11-01', 'analyst_count': None}]
    data = with_imports(dell(), import_file('consensus', rows))
    result = engine.run(data)
    assert result['expectations']['consensus_status'] == 'available'
    assert not [c for c in result['expectations']['comparisons'] if c['type'] == 'guidance_vs_consensus']
    gaap = [{**rows[0], 'accounting_basis': 'gaap', 'value': 6.2, 'estimate_date': '2026-11-02'}]
    result = engine.run(with_imports(dell(), import_file('consensus', gaap)))
    row = next(c for c in result['expectations']['comparisons'] if c['type'] == 'guidance_vs_consensus')
    assert row['difference_percent'] == pytest.approx((6.2 / 6.1 - 1) * 100)


# --- forecast and analytical gates ------------------------------------------------------------

def test_direct_eps_selection_without_drivers_fails():
    data = dell()
    legacy = [ScenarioProposal(entity_id='DELL', name=n, target_period=DELL_HORIZON, financial_outcome='x',
                               valuation_metric='eps', valuation_metric_value=v, multiple_method='pe', multiple=12,
                               multiple_basis='model_assumption', invalidation_conditions=['x'])
              for n, v in (('bear', 14), ('base', 20), ('bull', 25))]
    result = engine.run(data, proposal_for(data, legacy))
    assert gate(result, 'assumption_evidence_quality')['status'] == 'FAIL'


def test_extreme_history_is_not_extrapolated_mechanically():
    data = dell()
    implicit = [ScenarioProposal(entity_id='DELL', name=n, target_period=DELL_HORIZON, financial_outcome='x',
                                 drivers={'operating_margin': driver(7.5, 'historical_fact')}, valuation_metric='eps',
                                 multiple_method='pe', multiple=m, multiple_basis='model_assumption', invalidation_conditions=['x'])
                for n, m in (('bear', 10), ('base', 12), ('bull', 14))]
    result = engine.run(data, proposal_for(data, implicit))
    assert result['forecast']['drivers']['revenue_growth']['status'] == 'extreme'
    assert '極端' in gate(result, 'assumption_evidence_quality')['detail']


def test_historical_label_outside_history_and_unsupported_margin_fail():
    data = dell()
    bad = [scenario('bear', 0, 10), scenario('base', 15, 12, margin=12.0),   # 12% labelled historical, history is 7.5%
           scenario('bull', 25, 14, margin=14.0, margin_basis='model_assumption')]
    detail = gate(engine.run(data, proposal_for(data, bad)), 'assumption_evidence_quality')
    assert detail['status'] == 'FAIL' and '歷史區間' in detail['detail'] and '超過 5 個百分點' in detail['detail']


def test_missing_tax_data_without_an_assumption_fails():
    data = dell(drop={e.evidence_id for e in dell().evidence if e.financial_metric == 'income_before_tax'})
    result = engine.run(data, proposal_for(data))
    assert result['forecast']['drivers']['tax_rate']['status'] == 'not_found'
    assert '稅率' in gate(result, 'assumption_evidence_quality')['detail']


def test_missing_capex_leaves_fcf_unmodelled_but_eps_available():
    data = dell(drop={e.evidence_id for e in dell().evidence if e.financial_metric == 'capital_expenditure'})
    base = next(s for s in engine.run(data, proposal_for(data))['scenarios']['scenarios'] if s['name'] == 'base')
    assert base['statements']['eps'] is not None and base['statements']['free_cash_flow'] is None


def test_negative_earnings_make_pe_scenarios_unsuitable():
    data = dell()
    scen = [scenario('bear', 0, 10, margin=-5, margin_basis='model_assumption'), scenario('base', 15, 12),
            scenario('bull', 25, 14, margin=8.0, margin_basis='model_assumption')]
    assert gate(engine.run(data, proposal_for(data, scen)), 'valuation_method_suitability')['status'] == 'FAIL'


def test_method_outside_the_sector_profile_fails():
    data = dell()
    scen = default_scenarios()
    scen[1] = scenario('base', 15, 2.0, method='p_b')
    assert gate(engine.run(data, proposal_for(data, scen)), 'valuation_method_suitability')['status'] == 'FAIL'


def test_forecast_period_must_match_the_model_horizon():
    data = dell()
    scen = [scenario(n, g, m, period='FY2028') for n, g, m in (('bear', 0, 10), ('base', 15, 12), ('bull', 25, 14))]
    assert gate(engine.run(data, proposal_for(data, scen)), 'forecast_period_consistency')['status'] == 'FAIL'


def test_hypothetical_values_lose_false_precision():
    data = dell()
    result = engine.run(data, proposal_for(data))
    base = next(s for s in result['scenarios']['scenarios'] if s['name'] == 'base')
    assert base['implied_price_display'] != base['implied_price']
    assert len(str(int(base['implied_price_display'])).rstrip('0')) <= 2
    assert gate(result, 'scenario_uncertainty_disclosure')['status'] == 'PASS'


def test_gates_report_four_states():
    data = dell()
    statuses = {c['status'] for c in engine.run(data, proposal_for(data))['gates']['checks']}
    assert statuses <= {'PASS', 'WARNING', 'FAIL', 'NOT_APPLICABLE'} and 'WARNING' in statuses
    none = engine.run(data, proposal_for(data, []))
    assert gate(none, 'scenario_internal_consistency')['status'] == 'NOT_APPLICABLE'


# --- sector drivers ---------------------------------------------------------------------------

def test_hardware_drivers_compute_working_capital_days_and_flag_inventory_uncertainty():
    result = engine.run(dell())
    drivers = {d['name']: d for d in result['sector_drivers']['drivers']}
    assert {'inventory_days', 'receivable_days', 'payable_days', 'cash_conversion_cycle'} <= set(drivers)
    assert drivers['inventory_demand_assessment']['status'] == 'uncertain'
    cutoff = dell().request.research_as_of
    backlog = fin('DELL', cutoff, 'backlog', 95e9, None, date(2026, 7, 31), 'instant', 'USD', 'USD', evidence_id='bl')
    result = engine.run(dell(extra=[backlog]))
    drivers = {d['name']: d for d in result['sector_drivers']['drivers']}
    assert drivers['inventory_demand_assessment']['status'] == 'partially_supported'


def test_semiconductor_drivers_include_capital_intensity():
    from engine_fixtures import tsmc
    result = engine.run(tsmc())
    names = {d['name'] for d in result['sector_drivers']['drivers']}
    assert 'incremental_gross_margin' in names
    assert 'advanced_node_share' in result['sector_drivers']['unavailable']
