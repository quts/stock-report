"""Cash-statement reconciliation, rolling vintages and quarter/NTM aggregation regressions."""
from datetime import datetime, timedelta, timezone
import pytest
from stock_report import engine, forecast, fin_series
from stock_report.backtesting import snapshot, scores, METRICS
from stock_report.cashflow import reconcile
from engine_fixtures import dell, fin
from test_investment_engine import proposal_for, default_scenarios, gate


def bridge_series(other=5, movement=-20, ocf=95):
    """Four independently authored quarterly statement bridges, units in dollars."""
    data = dell()
    quarters = fin_series.build(data, 'DELL')['quarters']['revenue'][-4:]
    items = []
    for q in quarters:
        for metric,value in [('revenue', 1000), ('net_income', 100), ('depreciation_amortization', 10),
                             ('cf_other_noncash_adjustments', other), ('cf_working_capital_cash_movement', movement),
                             ('operating_cash_flow', ocf), ('capital_expenditure', 12)]:
            items.append(fin('DELL', data.request.research_as_of, metric, value, q['start'], q['end'], 'single_quarter', 'USD', 'USD'))
    data.evidence = items
    return fin_series.build(data, 'DELL')


def test_signed_statement_components_reconstruct_ocf_without_balance_proxies():
    series = bridge_series()
    r = reconcile(series)
    assert r['status'] == 'reconciled'
    assert r['net_income'] == 400 and r['da'] == 40
    assert r['noncash_adjustments'] == 20 and r['working_capital_cash_movement'] == -80
    assert r['modelled_ocf'] == r['actual_ocf'] == 380 and r['residual'] == 0
    series['instants']['accounts_payable'] = [{'value': 1e12}]  # Balance changes cannot alter the cash bridge.
    assert reconcile(series) == r


def test_residual_is_not_an_automatic_balancing_item_or_forecast_driver():
    r = reconcile(bridge_series(ocf=100))
    assert r['status'] == 'mismatch' and r['residual'] == 20
    data = dell()
    result = engine.run(data, proposal_for(data))
    a = result['forecast']
    d = {k: dict(v) for k,v in a['drivers'].items()}
    d['ocf_residual_intensity']['value'] = 999
    assert forecast.project(a['base'], d)['operating_cash_flow'] is None
    assert gate(result, 'cash_flow_statement_reconciliation')['status'] == 'FAIL'


def test_zero_residual_without_complete_statement_categories_stays_partial():
    series = bridge_series(other=0, movement=0, ocf=110)
    del series['quarters']['cf_working_capital_cash_movement']
    r = reconcile(series)
    assert r['residual'] == 0 and r['status'] == 'partial'
    assert r['working_capital_cash_movement'] is None


def test_statement_totals_do_not_double_count_details_and_reject_period_or_currency_mismatch():
    series = bridge_series()
    series['quarters']['cf_share_based_compensation'] = [dict(p, metric='cf_share_based_compensation', value=999)
                                                       for p in series['quarters']['cf_other_noncash_adjustments']]
    assert reconcile(series)['noncash_adjustments'] == 20
    for p in series['quarters']['cf_working_capital_cash_movement']:
        p['currency'] = 'TWD'
    assert reconcile(series)['status'] == 'partial'


def test_quarterly_guidance_is_applied_once_and_all_statements_aggregate():
    data = dell()
    result = engine.run(data, proposal_for(data))
    base = result['scenarios']['scenarios'][1]
    rows = base['quarterly_forecast']
    assert len(rows) == 4
    assert rows[0]['statements']['revenue'] == pytest.approx(49e9)
    assert sum(q['revenue_origin']['method'] == 'management_guidance_midpoint' for q in rows) == 1
    assert rows[1]['statements']['revenue'] != pytest.approx(49e9)
    for metric in ('revenue', 'operating_income', 'net_income', 'capital_expenditure'):
        assert base['statements'][metric] == pytest.approx(sum(q['statements'][metric] for q in rows))
    assert base['statements']['eps'] == pytest.approx(sum(q['statements']['eps'] for q in rows))
    assert gate(result, 'quarterly_forecast_consistency')['status'] == 'PASS'
    assert gate(result, 'guidance_period_alignment')['status'] == 'PASS'
    assert base['forecast_reliability'] == 'hypothetical_sensitivity'


def test_annual_guidance_ending_on_quarter_end_is_not_quarterly_guidance():
    data = dell()
    for e in data.evidence:
        if e.statement_kind == 'management_guidance' and e.financial_metric == 'revenue':
            e.target_period = 'FY2027'
    result = engine.run(data, proposal_for(data))
    base = result['scenarios']['scenarios'][1]
    assert all(q['revenue_origin']['method'] != 'management_guidance_midpoint' for q in base['quarterly_forecast'])
    assert base['statements']['guidance_rejected']


def test_annual_sensitivity_cannot_be_used_as_primary_base():
    data, scenarios = dell(), default_scenarios()
    scenarios[1].forecast_method = 'annual_sensitivity'
    result = engine.run(data, proposal_for(data, scenarios))
    assert gate(result, 'quarterly_forecast_consistency')['status'] == 'FAIL'


def test_snapshot_excludes_later_revisions_and_records_at_exact_cutoff():
    data = dell()
    origin = fin_series.build(data, 'DELL')['quarters']['revenue'][6]['end']
    cutoff = datetime.combine(origin + timedelta(days=45), datetime.min.time(), timezone.utc)
    original = next(e for e in data.evidence if e.financial_metric == 'revenue' and e.period_end == origin)
    revision = original.model_copy(update={'evidence_id': 'later-revision', 'value': original.value * 10,
                                          'published_at': cutoff, 'event_time': cutoff})
    data.evidence.append(revision)
    snap = snapshot(data, origin, cutoff)
    assert original.evidence_id in {e.evidence_id for e in snap.evidence}
    assert revision.evidence_id not in {e.evidence_id for e in snap.evidence}
    assert all(e.published_at < cutoff and e.event_time < cutoff for e in snap.evidence)
    assert snap.request.research_as_of == cutoff


def test_metric_coverage_bias_and_errors_have_separate_denominators():
    rows = [{'errors': {m: {'predicted': p, 'actual': 100, 'error_percent': p - 100 if p is not None else None}
                        for m in METRICS}} for p in (110, 80, None)]
    for v in scores(rows).values():
        assert v['sample_size'] == 2 and v['coverage'] == pytest.approx(2/3)
        assert v['mae'] == 15 and v['bias'] == -5
        assert v['mape_percent'] == 15 and v['bias_percent'] == -5


def test_revised_results_do_not_count_toward_point_in_time_samples():
    data = dell()
    # Move all financial publications after every historical cutoff, preserving current availability.
    for e in data.evidence:
        if e.kind == 'financial':
            e.published_at = data.request.research_as_of - timedelta(days=1)
            e.event_time = e.published_at
    bt = forecast.backtest(data, 'DELL')
    assert all(m['sample_size'] == 0 and m['coverage'] == 0 for m in bt['metrics'].values())
    assert bt['method'] == 'strict_point_in_time' and bt['reliability'] == 'not_validated'
    assert any(m['sample_size'] > 0 for m in bt['revised_diagnostic']['metrics'].values())


def test_actual_point_in_time_fixture_scores_each_metric_independently():
    data = dell()
    result = engine.run(data, proposal_for(data))
    bt = result['forecast']['backtest']
    assert bt['metrics']['revenue']['sample_size'] > 0
    assert bt['metrics']['operating_income']['sample_size'] > 0
    assert bt['metrics']['eps']['sample_size'] > 0
    assert bt['metrics']['free_cash_flow']['sample_size'] == 0
    assert gate(result, 'forecast_sample_sufficiency')['status'] == 'FAIL'
    first = bt['rows'][0]
    first['max_input_published_at'] = first['information_cutoff']
    from stock_report.quality_gates import forecast_reliability_checks
    assert next(g for g in forecast_reliability_checks(result) if g['check'] == 'point_in_time_backtest_integrity')['status'] == 'FAIL'


def test_quarterly_gate_detects_component_mismatch_and_wrong_guidance_period():
    data = dell()
    result = engine.run(data, proposal_for(data))
    base = result['scenarios']['scenarios'][1]
    base['statements']['depreciation_amortization'] *= 1.1
    base['quarterly_forecast'][0]['revenue_origin']['guidance_period_end'] = '2030-01-01'
    from stock_report.quality_gates import forecast_reliability_checks
    checks = {g['check']: g for g in forecast_reliability_checks(result)}
    assert checks['quarterly_forecast_consistency']['status'] == 'FAIL'
    assert checks['guidance_period_alignment']['status'] == 'FAIL'
