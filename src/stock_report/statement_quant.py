"""Deterministic latest-statement comparisons; quarter, YTD and balance dates never mix."""
from collections import defaultdict
from .policy import reportable

LABELS = {
    'revenue': '營收', 'gross_profit': '毛利', 'operating_income': '營業利益', 'net_income': '淨利',
    'diluted_eps': '稀釋 EPS', 'eps': '稀釋 EPS', 'operating_cash_flow': '營業現金流',
    'capital_expenditure': '資本支出', 'free_cash_flow': '一般自由現金流',
    'adjusted_free_cash_flow': '調整後自由現金流', 'cash_equivalents': '現金及約當現金',
    'accounts_receivable': '應收帳款', 'inventory': '存貨', 'inventories': '存貨',
    'accounts_receivable_net': '淨應收帳款',
    'financing_receivables': '淨融資應收', 'financing_receivables_net': '淨融資應收',
    'total_debt': '總債務', 'debt_current': '一年內到期債務', 'debt_noncurrent': '長期債務',
    'short_term_debt': '短期債務', 'long_term_debt': '長期債務',
    'isg_revenue': 'ISG 營收', 'isg_operating_income': 'ISG 營業利益',
    'csg_revenue': 'CSG 營收', 'csg_operating_income': 'CSG 營業利益',
    'ai_server_revenue': 'AI 伺服器營收',
    'diluted_eps_gaap': 'GAAP 稀釋 EPS', 'diluted_eps_non_gaap': 'Non-GAAP 稀釋 EPS',
}


def format_amount(value, unit, currency=None):
    if value is None:
        return '—'
    if unit in ('USD', 'TWD'):
        return (f'{value / 1e8:,.2f} 億' if abs(value) >= 1e8 else f'{value:,.2f} ') + {'USD': '美元', 'TWD': '元'}.get(currency or unit, unit)
    return f'{value:,.2f}' + {'per_share': ' 美元／股', 'percent': '%', 'million': ' 百萬'}.get(unit, '')


def statement_metrics(data):
    groups = defaultdict(list)
    for e in data.evidence:
        if (e.kind == 'financial' and e.financial_metric and e.value is not None and e.period_end
                and e.statement_kind == 'fact' and reportable(e, data.request)):
            groups[(e.entity_id, e.financial_metric, e.period_basis, e.unit, e.currency)].append(e)
    result = []
    for (entity, metric, basis, unit, currency), rows in sorted(groups.items(), key=lambda pair: str(pair[0])):
        rows = sorted(rows, key=lambda e: (e.period_end, e.evidence_id), reverse=True)
        current = rows[0]
        # Two competing values for one period are not silently selected.
        same_period = [e for e in rows if e.period_start == current.period_start and e.period_end == current.period_end]
        if len({e.value for e in same_period}) > 1:
            continue
        prior = None
        for e in rows[1:]:
            days = (current.period_end - e.period_end).days
            if basis == 'instant' and days > 0:
                prior = e; break
            if 300 <= days <= 430 and current.period_start and e.period_start:
                span_delta = abs((current.period_end - current.period_start).days - (e.period_end - e.period_start).days)
                if span_delta <= 14:
                    prior = e; break
        change = round((current.value / prior.value - 1) * 100, 6) if prior and prior.value > 0 else None
        result.append({'entity_id': entity, 'name': metric, 'label': LABELS.get(metric, metric),
            'basis': basis, 'unit': unit, 'currency': currency, 'period': current.period,
            'period_end': current.period_end.isoformat(), 'current_value': current.value,
            'prior_value': prior.value if prior else None, 'prior_period': prior.period if prior else None,
            'comparison': '對財年末' if basis == 'instant' else '年比', 'change_percent': change,
            'current_display': format_amount(current.value, unit, currency),
            'prior_display': format_amount(prior.value, unit, currency) if prior else '—',
            'evidence_ids': [current.evidence_id] + ([prior.evidence_id] if prior else []),
            'formula': '(current / comparable_prior - 1) * 100' if change is not None else None})
    # Profitability ratios use the same entity, period and basis with actual, positive revenue.
    for income in list(result):
        if income['name'] not in ('gross_profit', 'operating_income', 'net_income'):
            continue
        revenue = next((r for r in result if r['entity_id'] == income['entity_id'] and r['name'] == 'revenue'
                        and r['basis'] == income['basis'] and r['period_end'] == income['period_end']
                        and r['unit'] == income['unit'] and r['currency'] == income['currency']), None)
        if revenue and revenue['current_value'] > 0:
            result.append({'entity_id': income['entity_id'], 'name': income['name'] + '_margin',
                'basis': income['basis'], 'period_end': income['period_end'],
                'value': round(income['current_value'] / revenue['current_value'] * 100, 6),
                'unit': 'percent', 'formula': 'income / same-period revenue * 100',
                'evidence_ids': list(dict.fromkeys(income['evidence_ids'][:1] + revenue['evidence_ids'][:1]))})
    return result
