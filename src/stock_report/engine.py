"""Investment research engine: one deterministic pass shared by Taiwan and US equities.

series → analysis → valuation → expectations → completeness → scenarios → events → gates.
The model's proposal only contributes assumptions and interpretation; every number the report
shows is computed here from evidence.
"""
from datetime import date
from . import (candidates, charts, completeness, expectations, fcf_history, fin_analysis, fin_series, forecast, market_data,
               materiality, multiples, quality_gates, retrospective, scenarios, sector_drivers, stability, usefulness, valuation)
from .backtesting import MIN_SAMPLES
from .research_contract import canonical_period, fiscal_year_end_month

VERSION = '6'
INVESTOR_LABELS = {'bear': 'Bear', 'base': '參考路徑', 'bull': 'Bull'}


def _label_scenarios(scen, cands):
    """Base is a reference unless point-in-time evidence selected its revenue path.

    Even then only the revenue path is labelled backtested: margins, tax, shares and multiples
    remain reference assumptions, so the scenario as a whole stays a mechanical sensitivity."""
    selected = cands.get('selected')
    for row in scen['scenarios']:
        row['investor_label'] = INVESTOR_LABELS.get(row['name'], row['name'])
        rule = row.get('revenue_candidate')
        row['revenue_candidate_hierarchy'] = cands['candidates'][rule]['hierarchy'] if rule in cands['candidates'] else None
        row['hierarchy_scope'] = 'whole_scenario'
        if row['name'] == 'base' and selected and rule == selected:
            row['investor_label'] = 'Base（營收路徑經時點回測）'
            scen['base_role'] = 'revenue_path_backtested_other_inputs_reference'
    if not selected:
        scen['base_role'] = 'reference_not_most_likely'


def jsonable(value):
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def run(data, proposal=None, decision=None):
    if data.request.mode != 'stock':
        return None
    entity = data.entities[0]
    fy_end = fiscal_year_end_month(entity.entity_id, entity.exchange)
    series = fin_series.build(data, entity.entity_id)
    analysis = fin_analysis.analyse(data, series, entity.entity_id)
    for row in analysis['quarters']:
        row['label'] = canonical_period(row['start'], row['end'], 'single_quarter', fy_end)
    price_series = market_data.prices(data, entity)
    refs = multiples.references(data, series, price_series, entity.entity_id)
    values = valuation.value(data, series, entity, analysis, refs)
    anchor = forecast.anchors(series, analysis)
    anchor['calibration'] = forecast.calibration(anchor)
    anchor['backtest'] = forecast.backtest(data, entity.entity_id)
    expect = expectations.compare(data, series, entity.entity_id)
    complete = completeness.assess(data, series, analysis, values, expect, entity.entity_id)
    scen = scenarios.evaluate(data, proposal, values, entity, anchor, refs)
    from .forecast_sanity import assess as forecast_review
    for row in scen['scenarios']:
        row['sanity'] = forecast_review(anchor, row)
    # Shared quarterly candidates; guidance applies only in its own quarter and with verified FX.
    guidance = forecast.guidance_records(data, entity.entity_id)
    fx = next((r['guidance_fx_rate_used'] for r in scen['scenarios'] if r.get('guidance_fx_rate_used')), None)
    fx_ids = next((r['guidance_fx_evidence_ids'] for r in scen['scenarios'] if r.get('guidance_fx_rate_used')), [])
    anchor['candidates'] = candidates.build(anchor, guidance, fx)
    retro = retrospective.evaluate(data, entity.entity_id)
    candidates.select(anchor['candidates'], retro, MIN_SAMPLES)
    _label_scenarios(scen, anchor['candidates'])
    _, profile = fin_analysis.sector_profile(entity.entity_id)
    engine = {'version': VERSION, 'entity_id': entity.entity_id, 'currency': entity.currency,
              'fiscal_year_end_month': fy_end, 'series': series, 'analysis': analysis, 'valuation': values,
              'forecast': anchor, 'sector_drivers': sector_drivers.compute(series, analysis, profile),
              'market_data': {'status': price_series['status'], 'history_points': len(price_series['points']),
                              'diagnostics': price_series['diagnostics']},
              'expectations': expect, 'completeness': complete, 'scenarios': scen, 'events': materiality.rank(data)}
    engine['retrospective'] = retro
    engine['stability'] = stability.assess(data, entity.entity_id, guidance, fx, fx_ids)
    adjusted = [e.evidence_id for e in data.evidence if e.entity_id == entity.entity_id and e.kind == 'financial'
                and (e.accounting_basis in ('non_gaap', 'company_adjusted') or (e.financial_metric or '').startswith('adjusted_'))]
    history = fcf_history.history(series, adjusted)
    shares = (values['market'].get('shares') or {}).get('value')
    price = values['market']['price']
    paths = {n: c['ntm_revenue'] for n, c in anchor['candidates']['candidates'].items() if c['ntm_revenue'] is not None}
    engine['historical_fcf'] = {'history': history, 'sensitivity': fcf_history.sensitivity(
        history, paths, price.get('value') if price.get('status') == 'available' else None, shares)}
    engine['cashflow_mapping'] = fcf_history.mapping_coverage(data, series, entity.entity_id, anchor['reconciliation'])
    engine['sensitivity_ranking'] = usefulness.sensitivities(engine)
    engine['reverse_valuation'] = usefulness.reverse_valuation(engine)
    engine['sector_context'] = usefulness.sector_context(engine)
    engine['thesis'] = [t.model_dump(mode='json') for t in proposal.thesis if t.entity_id == entity.entity_id] if proposal else []
    engine['gates'] = quality_gates.run(data, proposal, engine, entity, decision) if proposal else None
    return engine


def charts_for(engine):
    rows = engine['analysis']['quarters']
    labels = [(r['label'] or r['end'].isoformat()).replace('FY20', 'FY').replace('-', '') for r in rows]
    scale = 1e8 if engine['currency'] == 'TWD' else 1e9
    unit = '億元' if engine['currency'] == 'TWD' else '十億美元'
    revenue = [r['revenue']['value'] / scale if r['revenue'] else None for r in rows]
    eps = [r['diluted_eps']['value'] if r['diluted_eps'] else None for r in rows]
    out = {'revenue_eps': charts.bars_and_line(labels, revenue, eps if any(eps) else None, f'單季營收（{unit}）', '稀釋 EPS'),
           'margins': charts.lines(labels, [('毛利率', [r['gross_margin'] for r in rows]),
                                            ('營業利益率', [r['operating_margin'] for r in rows]),
                                            ('淨利率', [r['net_margin'] for r in rows])])}
    scen = engine['scenarios']
    if scen['scenarios']:
        rows = [{**r, 'implied_price': r['implied_price_display']} for r in scen['scenarios']]
        price = engine['valuation']['market']['price']
        out['scenarios'] = charts.scenario_bars(rows, price.get('value') if price.get('status') == 'available' else None)
    return out


def forecast_qa(engine):
    """Machine-readable forecast QA kept out of the PDF: technical identifiers live here."""
    f = engine['forecast']
    retro = engine['retrospective']
    return {'engine_version': VERSION, 'entity_id': engine['entity_id'], 'horizon': f['horizon'],
            'hierarchy': {'levels': list(candidates.HIERARCHY), 'base_role': engine['scenarios'].get('base_role'),
                          'scenarios': {r['name']: {'hierarchy': r.get('hierarchy'), 'legacy_confidence_class': r.get('confidence_class'),
                                                    'revenue_candidate': r.get('revenue_candidate'),
                                                    'investor_label': r.get('investor_label'),
                                                    'quarter_hierarchy': [q.get('hierarchy') for q in r.get('quarterly_forecast', [])]}
                                        for r in engine['scenarios']['scenarios']}},
            'candidate_comparison': {'candidates': f['candidates'],
                                     'point_in_time': {k: v for k, v in retro['point_in_time'].items() if k != 'rows'},
                                     'revised_data': {k: v for k, v in retro['revised_data'].items() if k != 'rows'},
                                     'claims': retro['claims']},
            'point_in_time_rows': retro['point_in_time']['rows'], 'revised_data_rows': retro['revised_data']['rows'],
            'filing_dataset': retro['filing_dataset'], 'stability': engine['stability'],
            'historical_fcf': engine['historical_fcf'], 'cashflow_mapping': engine['cashflow_mapping'],
            'sensitivity_ranking': engine['sensitivity_ranking'], 'reverse_valuation': engine['reverse_valuation'],
            'sector_context': engine['sector_context'],
            'gate_ids': [{'check': c['check'], 'label': c.get('label'), 'status': c['status'], 'detail': c['detail']}
                         for c in (engine.get('gates') or {}).get('checks', [])]}


def public(engine):
    """JSON-safe engine output for audit files; series points keep their provenance IDs."""
    return jsonable(engine)
