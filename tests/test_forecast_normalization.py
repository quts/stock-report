"""Economic extrapolation, attribution, filing-vintage and confidence regressions."""
from datetime import datetime, timedelta, timezone
import pytest
from stock_report import engine, forecast, fin_series
from stock_report.backtesting import snapshot
from stock_report.forecast_attribution import change_report, compare_backtests
from stock_report.sources import build_adapter
from engine_fixtures import dell
from test_investment_engine import default_scenarios, proposal_for, gate


def level_case(data=None):
    data = data or dell()
    scenarios = default_scenarios()
    scenarios[1].growth_policy = 'level_carry_forward'
    scenarios[1].drivers['revenue_growth'].value = 0
    return data, engine.run(data, proposal_for(data, scenarios))


def test_level_reference_explains_high_base_effect_without_compounding_or_growth_cap():
    data,result = level_case()
    base = result['scenarios']['scenarios'][1]
    rows = base['quarterly_forecast']
    latest = result['series']['quarters']['revenue'][-1]['value']
    assert [q['statements']['revenue'] for q in rows] == pytest.approx([49e9, latest, latest, latest])
    assert rows[-1]['statements']['revenue_growth'] == pytest.approx(0)
    assert rows[1]['statements']['revenue_growth'] > rows[2]['statements']['revenue_growth'] > rows[3]['statements']['revenue_growth']
    assert '比較基期' in rows[-1]['revenue_origin']['limitation']
    assert base['statements']['revenue'] == pytest.approx(49e9 + 3*latest)


def test_counterfactual_attribution_exactly_explains_revenue_eps_and_multiple_changes():
    data = dell()
    previous = engine.run(data, proposal_for(data))
    _,current = level_case(data)
    a = change_report(current['forecast'], previous['scenarios']['scenarios'][1], current['scenarios']['scenarios'][1])
    diagnosis = a['inflation_diagnosis']
    assert diagnosis['uniform_yoy_ntm'] + diagnosis['guidance_override_total'] == pytest.approx(previous['scenarios']['scenarios'][1]['statements']['revenue'])
    assert diagnosis['not_compounded']
    actual = a['financial_changes']['eps']['change']
    assert sum(r['eps_effect'] for r in a['eps_sequential_bridge']) == pytest.approx(actual)
    assert a['valuation_change_bridge']['multiple_effect_at_new_eps'] == 0
    assert a['financial_changes']['free_cash_flow']['after'] is None


def test_strong_actual_quarter_guidance_is_not_capped_and_does_not_validate_whole_forecast():
    data = dell()
    for e in data.evidence:
        if e.statement_kind == 'management_guidance' and e.financial_metric == 'revenue':
            e.value = 1e12
    _,r = level_case(data)
    base = r['scenarios']['scenarios'][1]
    assert base['quarterly_forecast'][0]['statements']['revenue'] == 1e12
    assert base['quarterly_forecast'][0]['revenue_confidence'] == 'evidence_supported_forecast'
    assert base['confidence_class'] == 'mechanical_scenario'
    assert base['statements']['eps'] is not None
    assert gate(r, 'forecast_growth_sanity')['status'] == 'WARNING'
    assert not base['valuation_is_price_target']


def test_revenue_computation_does_not_require_two_yoy_pairs_or_profit_inputs():
    data = dell()
    data.evidence = [e for e in data.evidence if e.financial_metric != 'operating_income']
    from stock_report.fin_analysis import analyse
    s = fin_series.build(data, 'DELL')
    a = forecast.anchors(s, analyse(data, s, 'DELL'))
    d = forecast.trend_drivers(a)
    assert d['operating_margin']['value'] is None
    statement,quarters = forecast.quarterly_project(a, d)
    assert statement['revenue'] is not None and len(quarters) == 4
    assert statement['operating_income'] is None and statement['eps'] is None


def payload(value, filed, accession):
    return {'cik': 1571996, 'entityName': 'Dell Technologies Inc.', 'units': {'USD': [
        {'start': '2026-05-02', 'end': '2026-07-31', 'val': value, 'form': '10-Q',
         'filed': filed, 'accn': accession, 'fy': 2027, 'fp': 'Q2'}]}}


def test_adapter_preserves_original_and_amended_vintages_with_distinct_ids():
    adapter = build_adapter('sec-xbrl-companyconcept', {'series': 'quarterly'})
    old = payload(100, '2026-08-20', '0001571996-26-000001')
    new = payload(120, '2026-09-08', '0001571996-26-000046')
    old['units']['USD'].extend(new['units']['USD'])
    parsed = adapter.parse('DELL', {'revenue:Revenues': old, 'net_income:NetIncomeLoss': old}, datetime(2026,11,15,tzinfo=timezone.utc))
    rows = [r for r in parsed.observations if r.financial_metric == 'revenue']
    assert {r.value for r in rows} == {100,120}
    assert len({r.evidence_id for r in rows}) == 2 and all(len(r.evidence_id) <= 80 for r in rows)
    assert rows[0].published_at != rows[1].published_at


def test_cash_movement_mapping_preserves_finance_receivable_sign_and_does_not_use_balances():
    adapter = build_adapter('sec-xbrl-companyconcept', {'series': 'quarterly'})
    args = {'revenue:Revenues': payload(100, '2026-09-08', '0001571996-26-000046'),
            'net_income:NetIncomeLoss': payload(10, '2026-09-08', '0001571996-26-000046'),
            'cf_receivables:IncreaseDecreaseInAccountsReceivable': payload(20, '2026-09-08', '0001571996-26-000046'),
            'cf_financing_receivables:IncreaseDecreaseInFinanceReceivables': payload(-7, '2026-09-08', '0001571996-26-000046'),
            'cf_payables:IncreaseDecreaseInAccountsPayable': payload(30, '2026-09-08', '0001571996-26-000046')}
    result = adapter.parse('DELL', args, datetime(2026,11,15,tzinfo=timezone.utc))
    values = {e.financial_metric: e.value for e in result.observations}
    assert values['cf_receivables'] == -20 and values['cf_financing_receivables'] == 7
    assert values['cf_payables'] == 30
    assert 'accounts_receivable_net' not in values


def test_noncash_income_tag_is_reversed_to_the_statement_adjustment_sign():
    adapter = build_adapter('sec-xbrl-companyconcept', {'series': 'quarterly'})
    args = {'revenue:Revenues': payload(100, '2026-09-08', '0001571996-26-000046'),
            'net_income:NetIncomeLoss': payload(10, '2026-09-08', '0001571996-26-000046'),
            'cf_other_noncash:OtherNoncashIncomeExpense': payload(454, '2026-09-08', '0001571996-26-000046')}
    result = adapter.parse('DELL', args, datetime(2026, 11, 15, tzinfo=timezone.utc))
    row = next(e for e in result.observations if e.financial_metric == 'cf_other_noncash')
    assert row.value == -454



def test_series_revision_selection_is_clock_aware_instead_of_false_source_conflict():
    data = dell()
    original = next(e for e in data.evidence if e.financial_metric == 'revenue' and str(e.period_end) == '2025-08-01')
    revision = original.model_copy(update={'evidence_id': 'revision-test', 'value': original.value * 2,
                                          'published_at': original.published_at + timedelta(days=20)})
    data.evidence.append(revision)
    origin = original.period_end
    clock = original.published_at + timedelta(microseconds=1)
    past = fin_series.build(snapshot(data, origin, clock), 'DELL')
    assert past['quarters']['revenue'][-1]['value'] == original.value
    latest = fin_series.build(data, 'DELL')
    assert next(p for p in latest['quarters']['revenue'] if p['end'] == origin)['value'] == revision.value


def test_actual_publication_cutoff_can_follow_legacy_45_day_schedule_without_leaking():
    data = dell()
    original = next(e for e in data.evidence if e.financial_metric == 'revenue' and str(e.period_end) == '2025-05-02')
    original.published_at += timedelta(days=20)
    original.event_time = original.published_at
    bt = forecast.backtest(data, 'DELL')
    row = next(r for r in bt['rows'] if r['origin'] == original.period_end)
    assert datetime.fromisoformat(row['information_cutoff']) == original.published_at + timedelta(microseconds=1)
    assert 'legacy_fixed_45_day_cutoff_would_exclude_origin_filing' in row['diagnostics']['root_causes']
    assert row['max_input_published_at'] < datetime.fromisoformat(row['information_cutoff'])


def test_precision_and_sample_count_never_turn_a_mechanical_forecast_into_validated_forecast():
    data,result = level_case()
    base = result['scenarios']['scenarios'][1]
    assert base['display_significant_digits'] == 2
    assert base['implied_price_display'] != base['implied_price']
    for metric in result['forecast']['backtest']['metrics'].values():
        metric.update(sample_size=100, coverage=1)
    assert base['validation_status'] == 'not_validated'
    assert result['scenarios']['forecast_confidence'] == 'mechanical_scenario'
    base['confidence_class'] = 'validated_forecast'
    from stock_report.quality_gates import forecast_reliability_checks
    assert next(g for g in forecast_reliability_checks(result) if g['check'] == 'forecast_confidence_precision')['status'] == 'FAIL'


def test_backtest_comparison_uses_common_origins_and_actuals_not_changed_sample_mix():
    def row(origin, error, actual=100):
        return {'origin': origin, 'errors': {'revenue': {'predicted': actual + error, 'actual': actual, 'error_percent': error}}}
    old = {'revised_diagnostic': {'rows': [row('A', 20), row('B', 40)]}}
    new = {'revised_diagnostic': {'rows': [row('A', 30), row('B', 1, 200), row('C', 0)]}}
    result = compare_backtests(old, new)['revised_diagnostic']['revenue']
    assert result['previous_samples'] == 2 and result['current_samples'] == 3
    assert result['matched_samples'] == 1 and result['matched_origins'] == ['A']
    assert result['previous_matched_mape'] == 20 and result['current_matched_mape'] == 30
