"""Investment research engine: one deterministic pass shared by Taiwan and US equities.

series → analysis → valuation → expectations → completeness → scenarios → events → gates.
The model's proposal only contributes assumptions and interpretation; every number the report
shows is computed here from evidence.
"""
from datetime import date
from . import charts, completeness, expectations, fin_analysis, fin_series, materiality, quality_gates, scenarios, valuation
from .research_contract import canonical_period, fiscal_year_end_month

VERSION = '1'


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
    values = valuation.value(data, series, entity, analysis)
    expect = expectations.compare(data, series, entity.entity_id)
    complete = completeness.assess(data, series, analysis, values, expect, entity.entity_id)
    scen = scenarios.evaluate(data, proposal, values, entity)
    engine = {'version': VERSION, 'entity_id': entity.entity_id, 'currency': entity.currency,
              'fiscal_year_end_month': fy_end, 'series': series, 'analysis': analysis, 'valuation': values,
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
        out['scenarios'] = charts.scenario_bars(scen['scenarios'], engine['valuation']['market']['price'].get('value'))
    return out


def public(engine):
    """JSON-safe engine output for audit files; series points keep their provenance IDs."""
    return jsonable(engine)
