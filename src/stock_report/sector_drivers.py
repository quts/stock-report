"""Sector operating drivers computed from evidence; the sector profile selects which ones apply.

New industries add a profile entry naming drivers from DRIVERS (or a new function here), not
ticker-specific code.
"""
from .fin_series import latest_instant, ttm, year_ago


def _days(balance, flow_ttm):
    return None if balance is None or not flow_ttm else balance / flow_ttm * 365


def _cogs(series):
    rev, gp = ttm(series, 'revenue'), ttm(series, 'gross_profit')
    return None if rev is None or gp is None or rev['end'] != gp['end'] else rev['value'] - gp['value']


def _balance(series, names, end=None):
    for name in names:
        rows = [p for p in series['instants'].get(name, []) if end is None or p['end'] == end]
        if rows:
            return rows[-1]
    return None


def working_capital_days(series):
    rev, cogs = ttm(series, 'revenue'), _cogs(series)
    inv = latest_instant(series, 'inventory')
    if rev is None or inv is None:
        return []
    end = inv['end']
    ar = _balance(series, ('accounts_receivable_net', 'accounts_receivable'), end)
    ap = _balance(series, ('accounts_payable',), end)
    out = []
    dio = _days(inv['value'], cogs)
    dso = _days(ar['value'], rev['value']) if ar else None
    dpo = _days(ap['value'], cogs) if ap else None
    for name, value, ids in (('inventory_days', dio, inv['evidence_ids']), ('receivable_days', dso, ar['evidence_ids'] if ar else []),
                             ('payable_days', dpo, ap['evidence_ids'] if ap else [])):
        if value is not None:
            out.append({'name': name, 'value': value, 'unit': 'days', 'window': f'@{end}', 'evidence_ids': ids,
                        'formula': 'balance / TTM flow * 365（期末餘額對 TTM，非平均餘額）'})
    if None not in (dio, dso, dpo):
        out.append({'name': 'cash_conversion_cycle', 'value': dio + dso - dpo, 'unit': 'days', 'window': f'@{end}',
                    'evidence_ids': [], 'formula': 'inventory days + receivable days − payable days'})
    return out


def inventory_demand(series, analysis):
    """Is inventory growth backed by confirmed demand? Only answered when backlog data exists."""
    inv = latest_instant(series, 'inventory')
    backlog = latest_instant(series, 'backlog')
    growth = next((m for m in analysis['metrics'] if m['name'] == 'inventory_growth'), None)
    rev_growth = next((m for m in analysis['metrics'] if m['name'] == 'revenue_growth_yoy'), None)
    if inv is None or growth is None:
        return None
    text = f'存貨較一年前變動 {growth["value"]:+.1f}%'
    if rev_growth and rev_growth.get('value') is not None:
        text += f'，同期單季營收年增 {rev_growth["value"]:.1f}%'
    if backlog is None:
        return {'name': 'inventory_demand_assessment', 'status': 'uncertain',
                'text': text + '；沒有可比較的積壓訂單資料，無法判斷存貨增加是來自已確認的客戶需求或需求轉弱。'}
    prior = [p for p in series['instants'].get('backlog', []) if 300 <= (backlog['end'] - p['end']).days <= 400]
    ratio = backlog['value'] / inv['value'] if inv['value'] else None
    if not prior:
        return {'name': 'inventory_demand_assessment', 'status': 'partially_supported',
                'text': text + f'；期末積壓約為存貨的 {ratio:.1f} 倍，但缺少一年前的積壓可比，只能說明存貨有訂單覆蓋，無法確認覆蓋率是否改善。',
                'evidence_ids': backlog['evidence_ids'] + inv['evidence_ids']}
    backlog_growth = (backlog['value'] / prior[-1]['value'] - 1) * 100
    supported = backlog_growth >= growth['value']
    return {'name': 'inventory_demand_assessment', 'status': 'demand_backed' if supported else 'weakening_signal',
            'text': text + f'；積壓年增 {backlog_growth:+.1f}%，' + ('高於存貨增幅，存貨增加有訂單支撐。' if supported
                                                                 else '低於存貨增幅，需留意需求轉弱或交付延遲。'),
            'evidence_ids': backlog['evidence_ids'] + prior[-1]['evidence_ids'] + inv['evidence_ids']}


def ratio_driver(series, name, numerator, denominator, label):
    a, b = ttm(series, numerator), ttm(series, denominator)
    if a is None or b is None or a['end'] != b['end'] or not b['value']:
        return None
    return {'name': name, 'value': a['value'] / b['value'] * 100, 'unit': 'percent', 'window': f'TTM {a["start"]}~{a["end"]}',
            'evidence_ids': a['evidence_ids'] + b['evidence_ids'], 'formula': label}


def incremental_gross_margin(series):
    rev, gp = series['quarters'].get('revenue', []), series['quarters'].get('gross_profit', [])
    if not rev or not gp or rev[-1]['end'] != gp[-1]['end']:
        return None
    r0, g0 = year_ago(rev, rev[-1]), year_ago(gp, gp[-1])
    if r0 is None or g0 is None or rev[-1]['value'] == r0['value']:
        return None
    return {'name': 'incremental_gross_margin', 'value': (gp[-1]['value'] - g0['value']) / (rev[-1]['value'] - r0['value']) * 100,
            'unit': 'percent', 'window': f'{r0["end"]}→{rev[-1]["end"]}',
            'evidence_ids': rev[-1]['evidence_ids'] + gp[-1]['evidence_ids'] + r0['evidence_ids'] + g0['evidence_ids'],
            'formula': '（毛利年增額）／（營收年增額）：新增營收的毛利率'}


def latest_metric(series, name, label):
    p = (series['quarters'].get(name) or series['instants'].get(name) or [None])[-1]
    if p is None:
        return None
    return {'name': name, 'value': p['value'], 'unit': p['unit'], 'window': f'{p["start"]}~{p["end"]}',
            'evidence_ids': p['evidence_ids'], 'formula': label}


def growth_of(series, name, label):
    rows = series['instants'].get(name, [])
    if len(rows) < 2:
        return None
    a = rows[-1]
    prior = [p for p in rows if 300 <= (a['end'] - p['end']).days <= 400] or rows[:-1]
    b = prior[-1]
    if not b['value']:
        return None
    return {'name': f'{name}_growth', 'value': (a['value'] / b['value'] - 1) * 100, 'unit': 'percent',
            'window': f'{b["end"]}→{a["end"]}', 'evidence_ids': a['evidence_ids'] + b['evidence_ids'], 'formula': label}


DRIVERS = {
    'capex_intensity': lambda s, a: ratio_driver(s, 'capex_intensity', 'capital_expenditure', 'revenue', '資本支出／營收'),
    'depreciation_intensity': lambda s, a: ratio_driver(s, 'depreciation_intensity', 'depreciation_amortization', 'revenue',
                                                        '折舊攤銷／營收（新產能折舊對毛利率的壓力）'),
    'incremental_gross_margin': lambda s, a: incremental_gross_margin(s),
    'advanced_node_share': lambda s, a: latest_metric(s, 'advanced_node_share', '先進製程占晶圓營收比重'),
    'working_capital_days': lambda s, a: working_capital_days(s),
    'inventory_demand_assessment': lambda s, a: inventory_demand(s, a),
    'backlog': lambda s, a: latest_metric(s, 'backlog', '期末積壓訂單'),
    'orders': lambda s, a: latest_metric(s, 'orders', '單季訂單'),
    'financing_receivables_growth': lambda s, a: growth_of(s, 'financing_receivables_net', '融資應收較約一年前（或最近可比日）變動'),
}


def compute(series, analysis, profile):
    out, unavailable = [], []
    for name in profile.get('drivers', []):
        value = DRIVERS[name](series, analysis)
        if value is None or value == []:
            unavailable.append(name)
        elif isinstance(value, list):
            out.extend(value)
        else:
            out.append(value)
    return {'drivers': out, 'unavailable': unavailable}
