"""All numeric derivations are replayable Python calculations."""
import re
from .models import Evidence, Metric

VERSION = '1'

def usable(e: Evidence, cutoff) -> bool:
    return (e.verification_status == 'verified' and e.published_at <= cutoff
            and e.event_time <= cutoff and e.internal_use_allowed)

def comparable_years(current, prior):
    a, b = re.fullmatch(r'FY(\d{4})', current or ''), re.fullmatch(r'FY(\d{4})', prior or '')
    return bool(a and b and int(a[1]) == int(b[1]) + 1)

def calculate(evidence: list[Evidence], cutoff) -> list[Metric]:
    output = []
    for entity in sorted({e.entity_id for e in evidence if e.kind == 'identity'}):
        records = {e.metric_key: e for e in sorted(evidence, key=lambda x: (x.published_at, x.evidence_id))
                   if e.entity_id == entity and usable(e, cutoff) and e.value is not None and e.metric_key is not None}
        specs = [
            ('revenue_growth', 'revenue_current', 'revenue_prior', 'percent', '(current / prior - 1) * 100'),
            ('eps_growth', 'eps_current', 'eps_prior', 'percent', '(current / prior - 1) * 100'),
            ('net_margin', 'net_income', 'revenue_current', 'percent', 'net_income / revenue * 100'),
            ('free_cash_flow', 'operating_cash_flow', 'capital_expenditure', 'currency', 'operating_cash_flow - capital_expenditure'),
            ('pe', 'close', 'eps_current', 'multiple', 'close / trailing_12m_eps'),
        ]
        for name, a_key, b_key, unit, formula in specs:
            a, b = records.get(a_key), records.get(b_key)
            ids = [r.evidence_id for r in (a, b) if r]
            reason = None
            value = None
            if not a or not b:
                reason = '缺少可核實且截止日前公開的數值'
            elif not a.currency or a.currency != b.currency:
                reason = '幣別不一致，不可比較'
            elif name in ('revenue_growth', 'eps_growth') and (not comparable_years(a.period, b.period) or a.unit != b.unit):
                reason = '期別或單位不可比較'
            elif name in ('net_margin', 'free_cash_flow') and (a.period != b.period or a.unit != b.unit):
                reason = '期別或單位不可比較'
            elif name == 'pe' and (b.period != 'TTM' or b.value <= 0 or a.value <= 0 or a.unit != 'per_share' or b.unit != 'per_share'):
                reason = 'Not Applicable：需要正的 TTM EPS'
            elif name != 'free_cash_flow' and b.value <= 0:
                reason = 'Not Applicable：分母非正'
            elif name == 'free_cash_flow' and b.value < 0:
                reason = 'Not Applicable：資本支出須先正規化為正數'
            elif name == 'free_cash_flow':
                value = a.value - b.value
            elif name.endswith('growth'):
                value = (a.value / b.value - 1) * 100
            else:
                value = a.value / b.value * (100 if unit == 'percent' else 1)
            output.append(Metric(entity_id=entity, name=name, value=round(value, 6) if value is not None else None,
                                 unit=a.unit if a and unit == 'currency' else unit, currency=a.currency if a and unit == 'currency' else None,
                                 period=a.period if a else None, formula=formula, evidence_ids=ids, missing_reason=reason))
    return output

def consensus(evidence, entity_id, cutoff, mapping):
    latest = {}
    for e in evidence:
        if e.entity_id != entity_id or e.kind != 'rating' or not e.institution or not usable(e, cutoff):
            continue
        if not e.valid_until or e.valid_until < cutoff:
            continue
        key = e.institution.strip().casefold()
        if key not in latest or (e.published_at, e.evidence_id) > (latest[key].published_at, latest[key].evidence_id):
            latest[key] = e
    counts = {'Buy': 0, 'Hold': 0, 'Sell': 0, 'Unclassified': 0}
    rows = []
    for e in latest.values():
        table = mapping.get('institutions', {}).get(e.institution, mapping['default'])
        rating = table.get(e.raw_rating, 'Unclassified')
        counts[rating] += 1
        rows.append({'evidence_id': e.evidence_id, 'institution': e.institution, 'raw_rating': e.raw_rating,
                     'rating': rating, 'published_at': e.published_at.isoformat(), 'target_price': e.target_price})
    total = len(latest)
    return {'counts': counts, 'institutions': total, 'analysts': len({e.analyst for e in latest.values() if e.analyst}),
            'message_count': sum(e.kind == 'rating' and e.entity_id == entity_id and e.published_at <= cutoff for e in evidence),
            'ratios': {k: v / total if total else None for k, v in counts.items()}, 'rows': rows,
            'warning': '投顧共識資料不足' if total < 3 else '僅代表可核實樣本，非全市場共識'}
