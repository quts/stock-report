"""All numeric derivations are replayable Python calculations."""
import calendar
from datetime import date, timedelta
import re
import unicodedata
from .models import Evidence, Metric, PRICE_ANCHOR_TOLERANCE_DAYS, PRICE_CHANGE_MONTHS

VERSION = '2'

def usable(e: Evidence, cutoff) -> bool:
    return (e.verification_status == 'verified' and e.published_at <= cutoff
            and e.event_time <= cutoff and e.research_as_of == cutoff
            and e.license_status == 'confirmed' and e.internal_use_allowed)

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
    official = a.source_type != 'synthetic' or b.source_type != 'synthetic'
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
            or (eps.source_type != 'synthetic' and not has_window(eps)):
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

PRICE_CHANGE_FORMULA = ('(close / anchor_close - 1) * 100; price change only, dividends excluded, not a total return; '
                        'anchor = last completed session on or before the close date minus N calendar months')

def months_before(day: date, months: int) -> date:
    """Same day-of-month N calendar months earlier, clamped to that month's last day."""
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))

def trade_date(e):
    return e.period_end if e.period_basis == 'trading_day' and e.period_start == e.period_end else None

def price_change(entity, months, close, anchor):
    """Price change over N calendar months between two explicitly dated points of one series."""
    name, role = f'price_change_{months}m', f'close_anchor_{months}m'
    ids = [r.evidence_id for r in (close, anchor) if r]
    value, period, reason = None, None, None
    if not close or not anchor:
        missing = '、'.join(key for key, record in (('close', close), (role, anchor)) if not record)
        reason = f'缺少可核實且截止日前公開的價格點（{missing}）；不以其他日期或其他來源替代'
    elif not close.currency or close.currency != anchor.currency:
        reason = '幣別不一致，不可比較'
    elif close.unit != 'per_share' or anchor.unit != 'per_share':
        reason = '價格單位不是每股價格，不可比較'
    elif any(getattr(close, key) != getattr(anchor, key) for key in ('publisher', 'source_url', 'retrieved_at')):
        reason = '兩個價格點不屬於同一來源序列與同一次擷取，不跨來源或跨快照拼接'
    elif trade_date(close) is None or trade_date(anchor) is None:
        reason = '價格點缺少明確交易日，不以公開或擷取時間推定'
    elif close.price_adjustment != anchor.price_adjustment:
        reason = '兩個價格點的調整基礎不同，不可比較'
    elif close.price_adjustment in (None, 'unknown'):
        reason = 'Not Applicable：來源未文件化價格調整基礎（股票分割／股利），不推定跨期可比'
    elif close.price_adjustment == 'unadjusted':
        reason = 'Not Applicable：未調整價格無法排除區間內股票分割，不計算跨期變動'
    elif close.price_adjustment != 'split_adjusted':
        reason = 'Not Applicable：序列含股利調整，屬總報酬口徑，不作為價格變動呈現'
    else:
        target = months_before(trade_date(close), months)
        gap = (target - trade_date(anchor)).days
        period = f'{trade_date(anchor).isoformat()}→{trade_date(close).isoformat()}'
        if gap < 0 or gap > PRICE_ANCHOR_TOLERANCE_DAYS:
            reason = (f'基準交易日 {trade_date(anchor).isoformat()} 不在目標日 {target.isoformat()} 之前 '
                      f'{PRICE_ANCHOR_TOLERANCE_DAYS} 日內，不視為 {months} 個月前的價格')
        elif anchor.value <= 0 or close.value <= 0:
            reason = 'Not Applicable：基準或最新價格非正'
        else:
            value = round((close.value / anchor.value - 1) * 100, 6)
    return Metric(entity_id=entity, name=name, value=value, unit='percent', currency=None, period=period,
                  formula=PRICE_CHANGE_FORMULA, evidence_ids=ids, missing_reason=reason)

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
        for months in PRICE_CHANGE_MONTHS:
            output.append(price_change(entity, months, records.get('close'), records.get(f'close_anchor_{months}m')))
    return output

def rating_key(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').split()).casefold()


def rating_tables(mapping):
    """Validate configured three-way mappings without inventing institution definitions."""
    def table(raw):
        if not isinstance(raw, dict):
            raise ValueError('Rating mappings must be objects')
        result = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not rating_key(key) or value not in ('Buy', 'Hold', 'Sell'):
                raise ValueError('Rating mappings require nonempty labels and Buy/Hold/Sell values')
            normalized = rating_key(key)
            if normalized in result and result[normalized] != value:
                raise ValueError('Conflicting normalized rating mapping labels')
            result[normalized] = value
        return result
    default = table(mapping.get('default'))
    institutions = {}
    raw = mapping.get('institutions', {})
    if not isinstance(raw, dict):
        raise ValueError('Institution rating mappings must be an object')
    for name, values in raw.items():
        if not isinstance(name, str):
            raise ValueError('Institution mapping names must be strings')
        key = rating_key(name)
        if not key or key in institutions:
            raise ValueError('Duplicate or empty normalized institution mapping')
        institutions[key] = table(values)
    return default, institutions


def consensus(evidence, entity_id, cutoff, mapping):
    default, institutions = rating_tables(mapping)
    eligible = [e for e in evidence if e.entity_id == entity_id and e.kind == 'rating'
                and rating_key(e.institution) and usable(e, cutoff)
                and e.research_as_of == cutoff and e.license_status == 'confirmed'
                and e.valid_until and e.valid_until >= cutoff]
    groups = {}
    for e in eligible:
        groups.setdefault(rating_key(e.institution), []).append(e)
    counts = {'Buy': 0, 'Hold': 0, 'Sell': 0, 'Unclassified': 0}
    rows, current = [], []
    for key in sorted(groups):
        published = max(e.published_at for e in groups[key])
        tied = sorted((e for e in groups[key] if e.published_at == published), key=lambda e: e.evidence_id)
        current.extend(tied)
        e = tied[0]
        conflict = len({rating_key(r.raw_rating) for r in tied}) > 1
        table = {**default, **institutions.get(key, {})}
        rating = 'Unclassified' if conflict else table.get(rating_key(e.raw_rating), 'Unclassified')
        counts[rating] += 1
        prices = {r.target_price for r in tied}
        currencies = {r.currency for r in tied}
        price = next(iter(prices)) if len(prices) == 1 else None
        currency = next(iter(currencies)) if len(currencies) == 1 else None
        price = price if price is not None and price > 0 and currency in ('USD', 'TWD') else None
        rows.append({'evidence_id': e.evidence_id, 'institution': e.institution, 'raw_rating': e.raw_rating,
                     'raw_ratings': sorted({r.raw_rating or '' for r in tied}),
                     'evidence_ids': sorted({r.evidence_id for r in tied}), 'conflicting': conflict,
                     'rating': rating, 'published_at': e.published_at.isoformat(), 'target_price': price,
                     'target_price_currency': currency if price is not None else None})
    total = len(groups)
    return {'counts': counts, 'institutions': total,
            'analysts': len({(rating_key(e.institution), rating_key(e.analyst))
                             for e in current if rating_key(e.analyst)}),
            'message_count': len({e.evidence_id for e in eligible}),
            'conflicting_institutions': sum(row['conflicting'] for row in rows),
            'ratios': {k: v / total if total else None for k, v in counts.items()}, 'rows': rows,
            'warning': ('投顧共識資料不足' if total < 3 else '僅代表可核實樣本，非全市場共識')
                       + ('；同機構同時點評等衝突列為未分類' if any(row['conflicting'] for row in rows) else '')}
