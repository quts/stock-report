"""Investment research engine: one deterministic pass shared by Taiwan and US equities.

series → analysis → valuation → expectations → completeness → scenarios → events → gates.
The model's proposal only contributes assumptions and interpretation; every number the report
shows is computed here from evidence.
"""
from datetime import date
from . import (charts, completeness, expectations, fin_analysis, fin_series, forecast, market_data, materiality, multiples,
               quality_gates, scenarios, sector_drivers, valuation)
from .research_contract import canonical_period, fiscal_year_end_month

VERSION = '4'


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
    _, profile = fin_analysis.sector_profile(entity.entity_id)
    engine = {'version': VERSION, 'entity_id': entity.entity_id, 'currency': entity.currency,
              'fiscal_year_end_month': fy_end, 'series': series, 'analysis': analysis, 'valuation': values,
              'forecast': anchor, 'sector_drivers': sector_drivers.compute(series, analysis, profile),
              'market_data': {'status': price_series['status'], 'history_points': len(price_series['points']),
                              'diagnostics': price_series['diagnostics']},
              'expectations': expect, 'completeness': complete, 'scenarios': scen, 'events': materiality.rank(data)}
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


def public(engine):
    """JSON-safe engine output for audit files; series points keep their provenance IDs."""
    return jsonable(engine)
