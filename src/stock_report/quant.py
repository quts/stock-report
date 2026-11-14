"""All numeric derivations are replayable Python calculations."""
from datetime import timedelta
import re
from .models import Evidence, Metric

VERSION = '1'

def usable(e: Evidence, cutoff) -> bool:
    return (e.verification_status == 'verified' and e.published_at <= cutoff
            and e.event_time <= cutoff and e.internal_use_allowed)

def comparable_years(current, prior):
    a, b = re.fullmatch(r'FY(\d{4})', current or ''), re.fullmatch(r'FY(\d{4})', prior or '')
    return bool(a and b and int(a[1]) == int(b[1]) + 1)

def has_window(e):
    return e.period_start is not None and e.period_end is not None and e.period_basis is not None

def window_problem(a, b, name):
    """Why two figures may not be combined, judged on recorded windows rather than labels.

    Evidence without recorded windows (synthetic fixtures) keeps the label-only rule. Official
    evidence must carry its window, and once either side carries one both must agree: an equal
    label or end year is never enough to equate two reporting windows.
    """
    official = a.source_type == 'official' or b.source_type == 'official'
    if not has_window(a) and not has_window(b):
        return '官方財務證據缺少期間起訖，不以期別標籤推定可比' if official else None
    if not has_window(a) or not has_window(b):
        return '僅一方記錄期間起訖，不以期別標籤推定可比'
    if a.period_basis != b.period_basis:
        return f'期間基礎不同（{a.period_basis} 與 {b.period_basis}），不可比較'
    if name in ('revenue_growth', 'eps_growth'):
        if a.period_basis != 'fiscal_year':
            return f'期間基礎為 {a.period_basis}，來源未提供可比的前期同窗資料，不計算成長率'
        if b.period_end + timedelta(days=1) != a.period_start:
            return '前期與本期的期間起訖不相鄰，不因年度標籤相鄰而視為可比'
    elif (a.period_start, a.period_end) != (b.period_start, b.period_end):
        return '期間起訖不同，不因期別標籤或結束年度相同而視為同一期間'
    return None

def pe_problem(close, eps):
    if eps.period != 'TTM' or (has_window(eps) and eps.period_basis != 'trailing_twelve_months') \
            or (eps.source_type == 'official' and not has_window(eps)):
        return (f'Not Applicable：需要正的 TTM EPS；現有 EPS 期間為 {eps.period or "未標示"}'
                f'（{eps.period_basis or "未記錄期間基礎"}），不將年度、單季或累計 EPS 年化或冒充 TTM')
    if eps.value <= 0 or close.value <= 0 or close.unit != 'per_share' or eps.unit != 'per_share':
        return 'Not Applicable：需要正的 TTM EPS'
    return None

UNDECLARED_SCALE = 'source_scale_undeclared'

def scale_problem(a, b):
    """An undeclared monetary scale only cancels inside one statement snapshot.

    Two figures tagged with the same placeholder unit are not known to share a scale unless
    they are the same provider record, from the same publisher and URL, in the same retrieval.
    """
    if UNDECLARED_SCALE not in (a.unit, b.unit):
        return None
    same = all(getattr(a, name) == getattr(b, name)
               for name in ('publisher', 'source_url', 'source_record_id', 'retrieved_at'))
    return None if same else '來源未宣告金額級距，且兩值不屬於同一來源申報紀錄與同一次擷取，不假設級距相同'

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
                missing = '、'.join(key for key, record in ((a_key, a), (b_key, b)) if not record)
                reason = f'缺少可核實且截止日前公開的數值（{missing}）'
                present = a or b
                if present and present.period_basis == 'cumulative_ytd':
                    reason += f'；現有 {present.period} 為年初至今累計數，不年化、不以其他期間替代'
            elif not a.currency or a.currency != b.currency:
                reason = '幣別不一致，不可比較'
            elif name in ('revenue_growth', 'eps_growth') and (not comparable_years(a.period, b.period) or a.unit != b.unit):
                reason = '期別或單位不可比較'
            elif name in ('net_margin', 'free_cash_flow') and (a.period != b.period or a.unit != b.unit):
                reason = '期別或單位不可比較'
            elif scale_problem(a, b):
                reason = scale_problem(a, b)
            elif name == 'pe' and pe_problem(a, b):
                reason = pe_problem(a, b)
            elif name != 'pe' and window_problem(a, b, name):
                reason = window_problem(a, b, name)
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
