"""Investor-facing derived views: greatest sensitivities and reverse valuation.

Everything here is arithmetic on the engine's own outputs. Ranges come from candidate rules,
observed quarterly history or explicit mechanical bands; none is a probability, a confidence
interval or a target price.
"""
from .scenarios import SENSITIVITY_MULTIPLE


def _structure(base):
    s = base.get('statements') or {}
    d = base['drivers']
    nonop, tax = d['non_operating_ratio']['value'], d['tax_rate']['value']
    if not s.get('revenue') or s.get('eps') is None or not s.get('diluted_shares') or None in (nonop, tax):
        return None
    return {'revenue': s['revenue'], 'eps': s['eps'], 'margin': s['operating_margin'], 'nonop': nonop, 'tax': tax,
            'shares': s['diluted_shares'], 'eps_per_revenue': s['eps'] / s['revenue']}


def sensitivities(engine):
    """Rank the reference scenario's value swing by input, holding everything else fixed."""
    scen = engine['scenarios']
    base = next((r for r in scen['scenarios'] if r['name'] == 'base'), None)
    if not base or base['valuation']['metric'] != 'eps':
        return {'status': 'unavailable', 'reason': 'reference scenario lacks an EPS-based valuation', 'ranked': []}
    st = _structure(base)
    if st is None:
        return {'status': 'unavailable', 'reason': 'reference EPS structure incomplete', 'ranked': []}
    multiple = base['valuation']['multiple']
    items = []
    paths = [c for c in engine['forecast']['candidates']['candidates'].values() if c['ntm_revenue'] is not None]
    if paths:
        eps = [(c['ntm_revenue'] * st['eps_per_revenue'], c['name']) for c in paths]
        low, high = min(eps), max(eps)
        items.append({'input': 'revenue_path', 'label': '營收路徑（候選規則）', 'basis': 'candidate_rules_mechanical',
                      'low_case': low[1], 'high_case': high[1], 'eps_low': low[0], 'eps_high': high[0],
                      'value_low': low[0] * multiple, 'value_high': high[0] * multiple, 'evidence_ids': []})
    quarters = [q for q in engine['analysis']['quarters'] if q['operating_margin'] is not None][-8:]
    if quarters:
        lo = min(quarters, key=lambda q: q['operating_margin'])
        hi = max(quarters, key=lambda q: q['operating_margin'])
        def eps_at(margin):
            return st['revenue'] * (margin + st['nonop']) / 100 * (1 - st['tax'] / 100) / st['shares']
        ids = sorted({i for q in (lo, hi) for i in (q['revenue']['evidence_ids'] if q['revenue'] else [])})
        items.append({'input': 'operating_margin', 'label': '營業利益率（近八季實際區間）', 'basis': 'observed_quarterly_range',
                      'low_case': f'{lo["operating_margin"]:.1f}%', 'high_case': f'{hi["operating_margin"]:.1f}%',
                      'low_period': lo['end'], 'high_period': hi['end'],
                      'eps_low': eps_at(lo['operating_margin']), 'eps_high': eps_at(hi['operating_margin']),
                      'value_low': eps_at(lo['operating_margin']) * multiple, 'value_high': eps_at(hi['operating_margin']) * multiple,
                      'evidence_ids': ids})
    items.append({'input': 'multiple', 'label': f'本益比 ±{round((SENSITIVITY_MULTIPLE[-1] - 1) * 100)}%（機械區間）',
                  'basis': 'mechanical_band', 'low_case': multiple * SENSITIVITY_MULTIPLE[0], 'high_case': multiple * SENSITIVITY_MULTIPLE[-1],
                  'eps_low': st['eps'], 'eps_high': st['eps'], 'value_low': st['eps'] * multiple * SENSITIVITY_MULTIPLE[0],
                  'value_high': st['eps'] * multiple * SENSITIVITY_MULTIPLE[-1], 'evidence_ids': []})
    for item in items:
        item['value_swing'] = item['value_high'] - item['value_low']
    items.sort(key=lambda i: i['value_swing'], reverse=True)
    return {'status': 'computed', 'reference_multiple': multiple, 'reference_eps': st['eps'], 'ranked': items,
            'note': '一次只改變一項輸入；區間來自候選規則、近八季實際值或機械±區間，不是機率或信賴區間。'}


def reverse_valuation(engine):
    """What revenue path or margin the verified price requires at each scenario multiple."""
    price = engine['valuation']['market']['price']
    if price.get('status') != 'available':
        return {'status': 'unavailable', 'reason': 'no verified current price; reverse valuation not computed'}
    scen = engine['scenarios']
    base = next((r for r in scen['scenarios'] if r['name'] == 'base'), None)
    st = _structure(base) if base else None
    if st is None or base['valuation']['metric'] != 'eps':
        return {'status': 'unavailable', 'reason': 'reference EPS structure incomplete'}
    ttm = engine['forecast']['base']['revenue_ttm']['value']
    paths = {c['name']: c['ntm_revenue'] for c in engine['forecast']['candidates']['candidates'].values() if c['ntm_revenue'] is not None}
    rows = []
    for r in scen['scenarios']:
        if r['valuation']['metric'] != 'eps':
            continue
        multiple = r['valuation']['multiple']
        required_eps = price['value'] / multiple
        revenue = required_eps / st['eps_per_revenue']
        margin = required_eps * st['shares'] / (st['revenue'] * (1 - st['tax'] / 100)) * 100 - st['nonop']
        rows.append({'scenario': r['name'], 'multiple': multiple, 'required_eps': required_eps,
                     'required_ntm_revenue_at_reference_margin': revenue,
                     'required_growth_vs_ttm_percent': (revenue / ttm - 1) * 100,
                     'required_operating_margin_at_reference_revenue': margin,
                     'paths_meeting_requirement': sorted(n for n, v in paths.items() if v >= revenue)})
    return {'status': 'computed', 'price': price['value'], 'price_evidence_ids': price.get('evidence_ids', []),
            'reference_structure': {k: st[k] for k in ('margin', 'nonop', 'tax', 'shares')}, 'rows': rows,
            'note': '以核實參考價、固定股數與參考利潤結構反推；未折現、無必要報酬假設，不支持合理價或目標價。'}


# Sector drivers are context for the revenue paths. Where the conversion from a driver to revenue
# is not disclosed, the gap is named and no numeric conversion is manufactured.
SECTOR_GAPS = {
    'hardware_ai_infrastructure': [
        ('backlog', 'backlog_conversion_schedule_not_disclosed',
         '積壓與訂單已揭露，但轉成營收的時程、可取消性與毛利未揭露；不換算成未來營收。'),
        ('orders', 'orders_are_segment_scoped', '訂單只涵蓋 AI 伺服器，不是全公司訂單；不外推成全公司營收。')],
    'semiconductor_manufacturing': [
        ('capex_intensity', 'capacity_and_utilization_not_in_evidence',
         '資本支出與先進製程占比已揭露，但產能、利用率與新產能開出時程不在已查核證據中；不換算成未來營收。')],
}


def sector_context(engine):
    profile = engine['analysis'].get('profile')
    present = {d['name'] for d in engine['sector_drivers']['drivers'] if d.get('name') and d.get('value') is not None}
    gaps = [{'code': code, 'text': text, 'driver': name}
            for name, code, text in SECTOR_GAPS.get(profile, []) if name in present]
    return {'profile': profile, 'drivers_used_as_context': sorted(present), 'unavailable': engine['sector_drivers']['unavailable'],
            'gaps': gaps, 'numeric_conversion_to_revenue': False}
