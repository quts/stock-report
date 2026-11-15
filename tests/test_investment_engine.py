"""Investment research engine: shared framework for Taiwan and US equities."""
from datetime import date, datetime, timedelta, timezone
import json
import pytest
from stock_report import engine, fin_series
from stock_report.models import (Assumption, CitedParagraph, CompanyResearch, HorizonProposal, InvestmentThesis,
                                 Proposal, ScenarioProposal)
from stock_report.research_contract import canonical_period
from engine_fixtures import dell, ev, fin, tsmc


def metric(result, name):
    return next(m for m in result['analysis']['metrics'] if m['name'] == name)


def method(result, name):
    return next(r for r in result['valuation']['results'] if r['method'] == name)


def field(result, name):
    return next(f for f in result['completeness']['fields'] if f['field'] == name)


# --- market conventions --------------------------------------------------------------------

def test_us_and_taiwan_use_the_same_engine_with_their_own_conventions():
    us, tw = engine.run(dell()), engine.run(tsmc())
    assert us['currency'] == 'USD' and tw['currency'] == 'TWD'
    assert us['analysis']['quarters'][-1]['label'] == 'FY2027-Q2'      # fiscal year ends in January
    assert tw['analysis']['quarters'][-1]['label'] == 'FY2026-Q3'      # calendar fiscal year
    assert len(us['analysis']['quarters']) == len(tw['analysis']['quarters']) == 8
    assert method(us, 'pe_trailing')['status'] == method(tw, 'pe_trailing')['status'] == 'available'


def test_fiscal_and_calendar_labels_are_distinct():
    assert canonical_period(date(2026, 5, 2), date(2026, 7, 31), 'single_quarter', 1) == 'FY2027-Q2'
    assert canonical_period(date(2026, 5, 2), date(2026, 7, 31), 'single_quarter', None) == '2026-05-02~2026-07-31'
    assert canonical_period(None, date(2026, 7, 31), 'instant', 1) == '@2026-07-31'


def test_taiwan_price_recency_uses_exchange_time():
    tw = engine.run(tsmc())
    price = tw['valuation']['market']['price']
    assert price['trade_date'] == '2026-11-13' and price['trading_days_lag'] == 0 and price['status'] == 'available'


# --- time series and derived quarters -------------------------------------------------------

def test_single_quarters_are_derived_from_year_to_date_windows_and_ttm_is_contiguous():
    series = fin_series.build(dell(), 'DELL')
    ocf = series['quarters']['operating_cash_flow']
    q2 = next(p for p in ocf if p['end'] == date(2026, 7, 31))
    assert q2['derived'] and q2['value'] == pytest.approx(2.2e9) and 'cumulative_ytd' in q2['formula']
    ttm = fin_series.ttm(series, 'operating_cash_flow')
    assert ttm['value'] == pytest.approx(10.0e9) and ttm['includes_derived_quarters']


def test_per_share_figures_are_never_derived_by_subtraction():
    data = dell(extra=[fin('DELL', datetime(2026, 11, 15, 9, tzinfo=timezone.utc), 'diluted_eps', 9.0,
                           date(2026, 1, 31), date(2026, 7, 31), 'cumulative_ytd', 'per_share', 'USD')])
    eps = fin_series.build(data, 'DELL')['quarters']['diluted_eps']
    assert not any(p['derived'] for p in eps)


def test_adjusted_figures_never_enter_the_reported_series():
    cutoff = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
    adjusted = fin('DELL', cutoff, 'diluted_eps', 99.0, date(2026, 5, 2), date(2026, 7, 31), 'single_quarter',
                   'per_share', 'USD', accounting_basis='non_gaap', evidence_id='dell-eps-adjusted')
    eps = fin_series.build(dell(extra=[adjusted]), 'DELL')['quarters']['diluted_eps']
    assert 99.0 not in [p['value'] for p in eps]


# --- missing, conflicting and stale data --------------------------------------------------------

def test_missing_price_makes_valuation_unavailable_and_report_limited():
    result = engine.run(dell(price=None), proposal_for(dell(price=None)))
    assert result['valuation']['assessment'] == 'unavailable'
    assert all(r['status'] in ('source_unavailable', 'not_found') for r in result['valuation']['results'])
    assert field(result, 'price_latest')['status'] == 'not_found'
    assert result['gates']['grade'] == 'limited'
    assert {'price_recency', 'mandatory_fields'} <= set(result['gates']['critical_failures'])


def test_recorded_retrieval_failure_is_not_reported_as_absent_data():
    from stock_report.models import DataGap
    data = dell(price=None)
    data.data_gaps = [DataGap(field='price_latest', status='source_unavailable', reason='test: endpoint timed out',
                              sources_tried=['primary', 'fallback'], checked_at=data.request.research_as_of)]
    # price_latest is computed from evidence; the recorded gap explains the missing value.
    data.data_gaps.append(DataGap(field='peers', status='source_unavailable', reason='test: peer source down',
                                  sources_tried=['peer-feed'], checked_at=data.request.research_as_of))
    result = engine.run(data)
    assert field(result, 'peers')['status'] == 'source_unavailable'
    assert field(result, 'peers')['sources_tried'] == ['peer-feed']


def test_stale_price_is_flagged_and_blocks_a_complete_report():
    data = dell(price_day=date(2026, 11, 3))
    result = engine.run(data, proposal_for(data))
    assert result['valuation']['market']['price']['status'] == 'stale'
    assert method(result, 'pe_trailing')['status'] == 'stale'
    assert 'price_recency' in result['gates']['critical_failures']


def test_conflicting_values_for_one_window_are_excluded_and_reported():
    cutoff = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
    other = fin('DELL', cutoff, 'revenue', 40e9, date(2026, 5, 2), date(2026, 7, 31), 'single_quarter', 'USD', 'USD',
                evidence_id='dell-revenue-conflict', source_type='secondary')
    result = engine.run(dell(extra=[other]))
    assert result['series']['conflicts'] and result['series']['conflicts'][0]['metric'] == 'revenue'
    assert result['analysis']['quarters'][-1]['end'] != date(2026, 7, 31)


def test_missing_consensus_keeps_guidance_separate():
    result = engine.run(dell())
    assert result['expectations']['consensus_status'] == 'not_found'
    assert result['expectations']['guidance'] and method(result, 'pe_forward')['status'] == 'not_found'
    assert any('不能代替市場共識' in n for n in result['expectations']['notes'])


def test_unavailable_capex_means_no_fcf_or_p_fcf():
    drop = {e.evidence_id for e in dell().evidence if e.financial_metric == 'capital_expenditure'}
    result = engine.run(dell(drop=drop))
    assert metric(result, 'free_cash_flow')['status'] == 'not_found'
    assert method(result, 'p_fcf')['status'] == 'not_found'


def test_taiwan_year_to_date_cash_flow_is_not_annualised_into_valuation():
    result = engine.run(tsmc())
    assert metric(result, 'operating_cash_flow_to_net_income')['window'].startswith('YTD')
    assert method(result, 'p_fcf')['status'] == 'not_found'
    assert '不年化' in method(result, 'p_fcf')['limitations'][0] or 'TTM' in method(result, 'p_fcf')['limitations'][0]


# --- negative earnings and FCF, volatility, sectors ---------------------------------------------

def test_negative_earnings_make_pe_not_applicable():
    result = engine.run(dell(net_income=[-0.5] * 9))
    assert method(result, 'pe_trailing')['status'] == 'not_applicable'
    assert any('EPS 為負' in f for f in result['analysis']['flags'])


def test_negative_fcf_keeps_yield_but_not_multiple():
    cutoff = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
    capex = [fin('DELL', cutoff, 'capital_expenditure', v * 1e9, date(2025, 2, 1) if i < 4 else date(2026, 1, 31), end, basis,
                 'USD', 'USD', evidence_id=f'capex-{i}')
             for i, (end, basis, v) in enumerate([(date(2025, 5, 2), 'single_quarter', 5), (date(2025, 8, 1), 'cumulative_ytd', 10),
                                                  (date(2025, 10, 31), 'cumulative_ytd', 15), (date(2026, 1, 30), 'fiscal_year', 20),
                                                  (date(2026, 5, 1), 'single_quarter', 5), (date(2026, 7, 31), 'cumulative_ytd', 10)])]
    drop = {e.evidence_id for e in dell().evidence if e.financial_metric == 'capital_expenditure'}
    result = engine.run(dell(drop=drop, extra=capex))
    assert metric(result, 'free_cash_flow')['value'] < 0
    assert method(result, 'p_fcf')['status'] == 'not_applicable'
    assert method(result, 'fcf_yield')['status'] == 'available' and method(result, 'fcf_yield')['value'] < 0


def test_extreme_growth_is_flagged():
    result = engine.run(dell(revenue_scale=1.0, extra=[]))
    data = dell()
    for e in data.evidence:
        if e.financial_metric == 'revenue' and e.period_end == date(2026, 7, 31):
            e.value = 80e9
    result = engine.run(data)
    assert any('100%' in f for f in result['analysis']['flags'])


def test_financial_sector_uses_book_value_methods(tmp_path, monkeypatch):
    config = json.loads(open('config/sector-profiles.json').read())
    config['tickers']['DELL'] = 'financials'
    path = tmp_path / 'profiles.json'
    path.write_text(json.dumps(config))
    monkeypatch.setenv('STOCK_REPORT_SECTOR_PROFILES', str(path))
    cutoff = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
    data = dell(extra=[fin('DELL', cutoff, 'total_equity', 70e9, None, date(2026, 7, 31), 'instant', 'USD', 'USD',
                           evidence_id='bank-equity')], drop={'dell-total_equity-2026-07-31-instant'})
    result = engine.run(data)
    methods = {r['method'] for r in result['valuation']['results']}
    assert 'ev_ebitda' not in methods and 'p_fcf' not in methods and method(result, 'p_b')['status'] == 'available'
    assert field(result, 'free_cash_flow')['status'] == 'not_applicable'


# --- scenarios, labelling and narrative checks ---------------------------------------------------

DELL_HORIZON = 'NTM 2026-08-01~2027-07-31'


def driver(value, basis='model_assumption', ids=()):
    return Assumption(name='test driver', value=value, unit='%', basis=basis, evidence_ids=list(ids), rationale='test')


def scenario(name, growth, multiple, margin=7.5, basis='model_assumption', ids=(), mbasis='model_assumption',
             margin_basis='historical_fact', method='pe', period=DELL_HORIZON, extra=None):
    drivers = {'revenue_growth': driver(growth, basis, ids), 'operating_margin': driver(margin, margin_basis)}
    drivers.update(extra or {})
    return ScenarioProposal(entity_id='DELL', name=name, target_period=period, drivers=drivers,
                            financial_outcome='test', valuation_metric='eps', multiple_method=method,
                            multiple=multiple, multiple_basis=mbasis, invalidation_conditions=['test condition'])


def default_scenarios():
    return [scenario('bear', 0, 10, margin=7.0, margin_basis='model_assumption'), scenario('base', 15, 12),
            scenario('bull', 25, 14, margin=8.0, margin_basis='model_assumption')]


def proposal_for(data, scenarios=None, texts=('營收年增 57.7% 。',), outlook='insufficient', thesis_text='測試：無法判斷估值高低。'):
    ids = ['dell-revenue-2026-07-31-single_quarter', 'dell-revenue-2025-08-01-single_quarter']
    if data.request.ticker != 'DELL':
        ids = [e.evidence_id for e in data.evidence if e.financial_metric == 'revenue'][-2:]
    paragraphs = [CitedParagraph(text=t, evidence_ids=ids) for t in texts]
    research = CompanyResearch(entity_id=data.request.ticker, **{f: paragraphs for f in (
        'overview', 'key_takeaways', 'fundamentals', 'news', 'earnings_preview', 'monitoring')})
    thesis = InvestmentThesis(entity_id=data.request.ticker, market_pricing=CitedParagraph(text=thesis_text, evidence_ids=ids),
                              key_assumptions=paragraphs, invalidation=['test'])
    return Proposal(input_sha256='0' * 64, model='manual-test', company_research=[research], thesis=[thesis],
                    outlooks=[HorizonProposal(entity_id=data.request.ticker, horizon=h, outlook=outlook,
                                              supporting_evidence_ids=ids, interpretation='測試。')
                              for h in ('3-6m', '6-12m')],
                    scenarios=scenarios if scenarios is not None else default_scenarios())


def gate(result, name):
    return next(c for c in result['gates']['checks'] if c['check'] == name)


def test_scenarios_are_driver_based_and_reconcile():
    data = dell()
    result = engine.run(data, proposal_for(data))
    rows = {s['name']: s for s in result['scenarios']['scenarios']}
    base = rows['base']
    s = base['statements']
    revenue_ttm = (27.0 + 33.0 + 37.0 + 46.97) * 1e9
    assert s['revenue'] == pytest.approx(revenue_ttm * 1.15)
    assert s['operating_income'] == pytest.approx(s['revenue'] * 0.075)
    assert s['net_income'] == pytest.approx(s['pretax_income'] * (1 - base['drivers']['tax_rate']['value'] / 100))
    assert base['drivers']['tax_rate']['basis'] == 'historical_fact' and base['drivers']['tax_rate']['value'] == pytest.approx(18.0)
    assert s['eps'] == pytest.approx(s['net_income'] / 700e6)
    assert s['free_cash_flow'] == pytest.approx(s['operating_cash_flow'] - s['capital_expenditure'])
    assert base['implied_price'] == pytest.approx(s['eps'] * 12, rel=1e-6)
    assert base['change_vs_reference_percent'] == pytest.approx((s['eps'] * 12 / 150 - 1) * 100, abs=0.01)
    assert result['scenarios']['probabilities'] == 'not_assigned'
    assert result['scenarios']['valuation_basis'] == 'hypothetical_sensitivity'
    assert base['implied_price_display'] == pytest.approx(round(base['implied_price'], -1), abs=10)
    assert gate(result, 'scenario_internal_consistency')['status'] == 'PASS'
    assert gate(result, 'valuation_reference_quality')['status'] == 'WARNING'
    assert gate(result, 'assumption_evidence_quality')['status'] == 'WARNING'   # base growth is a model assumption
    assert result['gates']['grade'] == 'complete', result['gates']['critical_failures']


def test_scenario_order_and_unsupported_multiple_basis_are_rejected():
    data = dell()
    bad = [scenario('bear', 25, 14), scenario('base', 15, 12), scenario('bull', 0, 10, mbasis='peer_median')]
    result = engine.run(data, proposal_for(data, bad))
    assert result['scenarios']['status'] == 'invalid'
    assert any('順序' in p for p in result['scenarios']['problems'])
    assert any('peer_median' in p for p in result['scenarios']['problems'])
    assert 'valuation_assumption_disclosure' in result['gates']['critical_failures']


def test_guidance_cannot_be_labelled_as_consensus():
    data = dell()
    mislabelled = [scenario('bear', 0, 10), scenario('base', 15, 12, basis='consensus', ids=['dell-guidance-rev']),
                   scenario('bull', 25, 14)]
    result = engine.run(data, proposal_for(data, mislabelled))
    assert 'guidance_consensus_labeling' in result['gates']['critical_failures']


def test_unsupported_numbers_in_narrative_are_detected():
    data = dell()
    ok = engine.run(data, proposal_for(data))
    assert next(c for c in ok['gates']['checks'] if c['check'] == 'unsupported_numbers')['passed']
    invented = engine.run(data, proposal_for(data, texts=('目標價 333.3 元，營收 777.7 億，EPS 55.55，毛利率 91.9%。',)))
    check = next(c for c in invented['gates']['checks'] if c['check'] == 'unsupported_numbers')
    assert not check['passed'] and 'unsupported_numbers' in invented['gates']['critical_failures']


def test_valuation_judgement_without_reference_is_a_contradiction():
    data = dell()
    result = engine.run(data, proposal_for(data, thesis_text='目前股價明顯低估。'))
    assert 'section_contradictions' in result['gates']['critical_failures']


def test_direction_contradicting_base_scenario_is_detected():
    data = dell()
    proposal = proposal_for(data, [scenario('bear', 0, 6), scenario('base', 5, 7), scenario('bull', 25, 14)])
    from stock_report.models import Decision
    decision = Decision(status='APPROVED', outlooks=[o.model_copy(update={'outlook': 'bullish'}) for o in proposal.outlooks],
                        findings=[], policy_version='test')
    result = engine.run(data, proposal, decision)
    assert 'section_contradictions' in result['gates']['critical_failures']


def test_engine_is_deterministic():
    data = dell()
    a, b = engine.public(engine.run(data, proposal_for(data))), engine.public(engine.run(data, proposal_for(data)))
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_ttm_from_year_to_date_windows_and_combined_depreciation():
    cutoff = tsmc().request.research_as_of
    rows = [fin('2330.TW', cutoff, 'operating_cash_flow', v, s, e, b, 'TWD', 'TWD', evidence_id=f'ocf-{i}')
            for i, (v, s, e, b) in enumerate([(2275e9, date(2025, 1, 1), date(2025, 12, 31), 'fiscal_year'),
                                              (1549e9, date(2025, 1, 1), date(2025, 9, 30), 'cumulative_ytd'),
                                              (2402e9, date(2026, 1, 1), date(2026, 9, 30), 'cumulative_ytd')])]
    rows += [fin('2330.TW', cutoff, m, v, date(2026, 1, 1), date(2026, 9, 30), 'cumulative_ytd', 'TWD', 'TWD',
                 evidence_id=f'{m}-9m') for m, v in (('depreciation', 577e9), ('amortization', 7e9))]
    data = tsmc()
    data.evidence = [e for e in data.evidence if e.financial_metric != 'operating_cash_flow'] + rows
    series = fin_series.build(data, '2330.TW')
    ttm = fin_series.ttm(series, 'operating_cash_flow')
    assert ttm['value'] == pytest.approx(3128e9) and ttm['start'] == date(2025, 10, 1) and ttm['end'] == date(2026, 9, 30)
    assert series['cumulative']['depreciation_amortization'][0]['value'] == pytest.approx(584e9)


def test_dates_are_not_treated_as_financial_numbers():
    from stock_report.quality_gates import numbers_in
    assert numbers_in('10/15公布，除息日2027/3/11，2026年11月10日董事會') == []
    assert [v for _, v in numbers_in('營收 14,942.4 億元')] == [14942.4]
    assert [v for _, v in numbers_in('新台幣14,942.4億元，股價2,350元，FY2027')] == [14942.4, 2350.0]


def test_eps_ttm_never_subtracts_cumulative_eps_and_falls_back_to_net_income_per_share():
    cutoff = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
    data = dell(drop={'dell-diluted_eps-2026-01-30-single_quarter'}, extra=[
        fin('DELL', cutoff, 'diluted_eps', 9.9, date(2025, 2, 1), date(2026, 1, 30), 'fiscal_year', 'per_share', 'USD',
            evidence_id='eps-fy'),
        fin('DELL', cutoff, 'diluted_eps', 7.0, date(2025, 2, 1), date(2025, 10, 31), 'cumulative_ytd', 'per_share', 'USD',
            evidence_id='eps-9m')])
    series = fin_series.build(data, 'DELL')
    assert fin_series.ttm(series, 'diluted_eps') is None
    result = engine.run(data)
    eps = metric(result, 'eps_ttm')
    assert eps['status'] == 'available' and 'net income' in eps['formula']
    assert eps['value'] == pytest.approx((1.5 + 1.7 + 2.6 + 4.13) * 1e9 / 700e6)
    assert field(result, 'shares')['status'] == 'available'


def test_sec_quarterly_series_mode_keeps_windows_separate():
    from stock_report.sources import build_adapter
    adapter = build_adapter('sec-xbrl-companyconcept', {'series': 'quarterly'})
    def payload(rows, unit='USD'):
        return {'cik': 1571996, 'entityName': 'Dell Technologies Inc.', 'units': {unit: rows}}
    rev = [{'start': '2026-05-02', 'end': '2026-07-31', 'val': 46.97e9, 'form': '10-Q', 'filed': '2026-09-08', 'accn': '0001571996-26-000046', 'fy': 2027, 'fp': 'Q2'},
           {'start': '2026-01-31', 'end': '2026-07-31', 'val': 90.8e9, 'form': '10-Q', 'filed': '2026-09-08', 'accn': '0001571996-26-000046', 'fy': 2027, 'fp': 'Q2'},
           {'start': '2026-05-02', 'end': '2026-07-31', 'val': 46.0e9, 'form': '10-Q', 'filed': '2026-08-20', 'accn': '0001571996-26-000001', 'fy': 2027, 'fp': 'Q2'}]
    cash = [{'end': '2026-07-31', 'val': 11.6e9, 'form': '10-Q', 'filed': '2026-09-08', 'accn': '0001571996-26-000046', 'fy': 2027, 'fp': 'Q2'}]
    result = adapter.parse('DELL', {'revenue:RevenueFromContractWithCustomerExcludingAssessedTax': payload(rev),
                                    'net_income:NetIncomeLoss': payload(rev),
                                    'cash_equivalents:CashAndCashEquivalentsAtCarryingValue': payload(cash)},
                           datetime(2026, 11, 15, tzinfo=timezone.utc))
    revenue = [o for o in result.observations if o.financial_metric == 'revenue']
    assert {o.period_basis for o in revenue} == {'single_quarter', 'cumulative_ytd'}
    assert next(o for o in revenue if o.period_basis == 'single_quarter').value == 46.97e9   # latest filing wins
    instant = next(o for o in result.observations if o.financial_metric == 'cash_equivalents')
    assert instant.period_basis == 'instant' and instant.period_start == instant.period_end
    assert all(o.accounting_basis == 'gaap' for o in revenue)
