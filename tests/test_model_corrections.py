"""Economic regression cases, separate from statement arithmetic identities."""
from datetime import date, datetime, timedelta
import pytest
from stock_report import engine, forecast, fin_series
from engine_fixtures import dell
from test_investment_engine import default_scenarios, proposal_for, gate, driver


def test_quarterly_guidance_cannot_be_claimed_as_direct_ntm_growth():
    data = dell()
    s = default_scenarios()
    a = s[1].drivers['revenue_growth']
    guidance = next(e for e in data.evidence if e.statement_kind == 'management_guidance' and e.financial_metric == 'revenue')
    a.basis, a.evidence_ids = 'management_guidance', [guidance.evidence_id]
    result = engine.run(data, proposal_for(data, s))
    assert gate(result, 'forecast_input_provenance')['status'] == 'FAIL'
    a.basis = 'guidance_informed_model'
    a.source_period, a.forecast_period = guidance.target_period, s[1].target_period
    a.accounting_basis, a.derivation = 'reported', 'Quarterly run rate held flat for four quarters; no seasonality.'
    assert gate(engine.run(data, proposal_for(data, s)), 'forecast_input_provenance')['status'] == 'PASS'
    a.forecast_period = 'FY2030'
    assert gate(engine.run(data, proposal_for(data, s)), 'forecast_input_provenance')['status'] == 'FAIL'


def test_residual_reconciles_actual_ocf_but_is_not_validation():
    result = engine.run(dell(), proposal_for(dell()))
    r = result['forecast']['reconciliation']
    assert r['modelled_ocf'] + r['residual'] == pytest.approx(r['actual_ocf'])
    assert r['net_income'] + r['da'] - sum(r['wc_components'].values()) == pytest.approx(r['modelled_ocf'])
    assert gate(result, 'ocf_reconciliation')['status'] == 'WARNING'
    assert result['forecast']['drivers']['ocf_residual_intensity']['basis'] == 'model_assumption'
    drivers = result['scenarios']['scenarios'][1]['drivers']
    drivers['ocf_residual_intensity']['value'] = None
    p = forecast.project(result['forecast']['base'], drivers)
    assert p['free_cash_flow'] is None and p['eps'] is not None


def test_missing_ap_or_stale_cashflow_cannot_be_silently_reconciled():
    data = dell()
    data.evidence = [e for e in data.evidence if e.financial_metric != 'accounts_payable']
    result = engine.run(data, proposal_for(data))
    assert result['forecast']['reconciliation']['status'] == 'unavailable'
    assert all(r['statements']['free_cash_flow'] is None for r in result['scenarios']['scenarios'])


def test_weak_or_already_breached_invalidation_fails():
    data, s = dell(), default_scenarios()
    s[1].operating_invalidations = []
    assert gate(engine.run(data, proposal_for(data, s)), 'scenario_invalidation_logic')['status'] == 'FAIL'
    s = default_scenarios()
    s[1].operating_invalidations[0].threshold = 100
    assert gate(engine.run(data, proposal_for(data, s)), 'scenario_invalidation_logic')['status'] == 'FAIL'
    s = default_scenarios()
    s[1].operating_invalidations[0].threshold = s[1].drivers['revenue_growth'].value - 0.0001
    assert gate(engine.run(data, proposal_for(data, s)), 'scenario_invalidation_logic')['status'] == 'FAIL'


def test_unsupported_multiple_remains_hypothetical_and_reverse_requires_price():
    data = dell()
    result = engine.run(data, proposal_for(data))
    base = result['scenarios']['scenarios'][1]
    rv = base['reverse_valuation']
    assert rv['required_per_share'] == pytest.approx(base['reference_price'] / base['valuation']['multiple'])
    assert result['scenarios']['valuation_basis'] == 'hypothetical_sensitivity'
    s = default_scenarios()
    s[1].multiple_basis = 'peer_median'
    assert gate(engine.run(data, proposal_for(data, s)), 'valuation_reference_quality')['status'] == 'FAIL'
    missing = dell(price=None)
    assert all(r['reverse_valuation'] is None for r in engine.run(missing, proposal_for(missing))['scenarios']['scenarios'])


def test_operating_contradiction_is_not_hidden_by_larger_bull_multiple():
    data, s = dell(), default_scenarios()
    s[2].drivers['revenue_growth'].value = -10
    assert gate(engine.run(data, proposal_for(data, s)), 'operating_conclusion_consistency')['status'] == 'FAIL'


def test_stale_forecast_component_is_unavailable():
    data = dell()
    series = fin_series.build(data, 'DELL')
    latest = series['quarters']['revenue'][-1]['end']
    series['quarters']['depreciation_amortization'] = [p for p in series['quarters']['depreciation_amortization'] if p['end'] < latest]
    series['cumulative']['depreciation_amortization'] = []
    from stock_report.fin_analysis import analyse
    a = forecast.anchors(series, analyse(data, series, 'DELL'))
    assert a['drivers']['da_intensity']['value'] is None


def test_missing_nonoperating_input_with_explicit_tax_does_not_become_zero_or_crash():
    data = dell()
    data.evidence = [e for e in data.evidence if e.financial_metric != 'income_before_tax']
    scenarios = default_scenarios()
    for s in scenarios:
        s.drivers['tax_rate'] = driver(18)
    result = engine.run(data, proposal_for(data, scenarios))
    assert all(r['statements']['pretax_income'] is None and r['statements']['eps'] is None
               for r in result['scenarios']['scenarios'])


def test_backtest_publication_precedes_target_and_never_uses_later_comparatives():
    bt = forecast.backtest(dell(), 'DELL')
    for r in bt['rows']:
        if r['information_cutoff'] is None:
            assert r['vintage_status'] == 'current_vintage_period_holdout'
            continue
        cutoff = datetime.fromisoformat(r['information_cutoff']).date()
        assert cutoff < r['target_end']
        assert cutoff <= r['origin'] + timedelta(days=120)
