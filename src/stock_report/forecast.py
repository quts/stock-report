"""Driver-based financial forecast for the next twelve months after the latest reported quarter.

Revenue → operating margin → operating income → non-operating items → pre-tax income → tax →
net income → diluted EPS, and net income + D&A − working-capital investment → OCF − capex → FCF.
Every driver carries its origin. Drivers the proposal leaves out fall back to the deterministic
historical anchor; anchors that cannot be measured are reported, never invented.
"""
from datetime import timedelta
from statistics import median
from .fin_series import ttm, year_ago

DRIVERS = ('revenue_growth', 'operating_margin', 'non_operating_ratio', 'tax_rate', 'share_change',
           'da_intensity', 'capex_intensity', 'working_capital_intensity')
EXTREME_GROWTH = 30.0


def _pct(a, b):
    return None if a is None or not b else a / b * 100


def anchors(series, analysis):
    rev, op, ni = ttm(series, 'revenue'), ttm(series, 'operating_income'), ttm(series, 'net_income')
    pretax, da, capex = ttm(series, 'income_before_tax'), ttm(series, 'depreciation_amortization'), ttm(series, 'capital_expenditure')
    shares = (series['quarters'].get('diluted_shares') or [None])[-1]
    quarters = analysis['quarters']
    growth = [q['revenue_yoy'] for q in quarters if q['revenue_yoy'] is not None]
    margins = [q['operating_margin'] for q in quarters if q['operating_margin'] is not None]
    out, notes = {}, []

    def put(name, value, rationale, ids=(), status='available'):
        out[name] = {'value': None if value is None else round(value, 4), 'basis': 'historical_fact',
                     'rationale': rationale, 'evidence_ids': sorted(set(ids)), 'status': status if value is not None else 'not_found'}
    recent = growth[-4:]
    g = median(recent) if recent else None
    put('revenue_growth', g, f'最近 {len(recent)} 季營收年增率中位數', rev['evidence_ids'] if rev else ())
    if g is not None and g > EXTREME_GROWTH:
        out['revenue_growth']['status'] = 'extreme'
        notes.append(f'歷史營收年增率中位數 {g:.1f}% 屬極端成長，不可直接外推；需有指引或明確假設')
    put('operating_margin', _pct(op['value'], rev['value']) if op and rev else None, 'TTM 營業利益率',
        (op['evidence_ids'] + rev['evidence_ids']) if op and rev else ())
    if pretax and op and rev:
        put('non_operating_ratio', _pct(pretax['value'] - op['value'], rev['value']), 'TTM（稅前淨利 − 營業利益）／營收',
            pretax['evidence_ids'] + op['evidence_ids'])
    else:
        put('non_operating_ratio', None, '缺少 TTM 稅前淨利，無法量測業外損益')
    if pretax and ni and pretax['value'] > 0:
        put('tax_rate', (1 - ni['value'] / pretax['value']) * 100, 'TTM 1 − 淨利／稅前淨利（含少數股權等項目）',
            pretax['evidence_ids'] + ni['evidence_ids'])
    else:
        put('tax_rate', None, '缺少 TTM 稅前淨利，無法量測有效稅率')
    put('share_change', 0.0 if shares else None, '以最新一季加權平均稀釋股數為基準', shares['evidence_ids'] if shares else ())
    put('da_intensity', _pct(da['value'], rev['value']) if da and rev else None, 'TTM 折舊攤銷／營收',
        (da['evidence_ids'] + rev['evidence_ids']) if da and rev else ())
    put('capex_intensity', _pct(capex['value'], rev['value']) if capex and rev else None, 'TTM 資本支出／營收',
        (capex['evidence_ids'] + rev['evidence_ids']) if capex and rev else ())
    wc = next((m for m in analysis['metrics'] if m['name'] == 'working_capital_change' and m['status'] == 'available'), None)
    prior_rev = None
    if rev:
        quarters_rev = series['quarters'].get('revenue', [])
        prior = [year_ago(quarters_rev, p) for p in quarters_rev[-4:]]
        if all(prior):
            prior_rev = sum(p['value'] for p in prior)
    if wc and rev and prior_rev and rev['value'] != prior_rev:
        put('working_capital_intensity', wc['value'] / (rev['value'] - prior_rev) * 100,
            '一年營運資金增加／同期 TTM 營收增加', wc['evidence_ids'] + rev['evidence_ids'])
    else:
        put('working_capital_intensity', None, '缺少可比的營運資金或前一年 TTM 營收')
    ranges = {'revenue_growth': (min(growth), max(growth)) if growth else None,
              'operating_margin': (min(margins), max(margins)) if margins else None}
    base = {'revenue_ttm': rev, 'operating_income_ttm': op, 'net_income_ttm': ni, 'pretax_ttm': pretax,
            'ocf_ttm': ttm(series, 'operating_cash_flow'), 'shares': shares}
    horizon = None
    if rev:
        start, end = rev['end'] + timedelta(days=1), rev['end'] + timedelta(days=365)
        horizon = {'start': start.isoformat(), 'end': end.isoformat(), 'label': f'NTM {start.isoformat()}~{end.isoformat()}'}
    return {'drivers': out, 'ranges': ranges, 'base': base, 'notes': notes, 'horizon': horizon}


def project(base, drivers):
    """Deterministic statements for one scenario; returns None for fields whose inputs are missing."""
    rev0, shares = base['revenue_ttm'], base['shares']
    d = {k: (v['value'] if v and v.get('value') is not None else None) for k, v in drivers.items()}
    if rev0 is None or d.get('revenue_growth') is None or d.get('operating_margin') is None:
        return None
    revenue = rev0['value'] * (1 + d['revenue_growth'] / 100)
    op = revenue * d['operating_margin'] / 100
    pretax = op + revenue * (d.get('non_operating_ratio') or 0) / 100
    tax = d.get('tax_rate')
    ni = pretax * (1 - tax / 100) if tax is not None else None
    share_count = shares['value'] * (1 + (d.get('share_change') or 0) / 100) if shares else None
    eps = ni / share_count if ni is not None and share_count else None
    da = revenue * d['da_intensity'] / 100 if d.get('da_intensity') is not None else None
    wc = (revenue - rev0['value']) * d['working_capital_intensity'] / 100 if d.get('working_capital_intensity') is not None else 0.0
    ocf = ni + da - wc if ni is not None and da is not None else None
    capex = revenue * d['capex_intensity'] / 100 if d.get('capex_intensity') is not None else None
    fcf = ocf - capex if ocf is not None and capex is not None else None
    return {'revenue': revenue, 'revenue_growth': d['revenue_growth'], 'operating_margin': d['operating_margin'],
            'operating_income': op, 'pretax_income': pretax, 'net_income': ni, 'diluted_shares': share_count,
            'eps': eps, 'depreciation_amortization': da, 'working_capital_investment': wc, 'operating_cash_flow': ocf,
            'capital_expenditure': capex, 'free_cash_flow': fcf,
            'fcf_per_share': fcf / share_count if fcf is not None and share_count else None,
            'ebitda': op + da if da is not None else None}


def calibration(anchor):
    """How well NI + D&A − ΔWC reproduces actual TTM operating cash flow (model fit, disclosed)."""
    base = anchor['base']
    d = anchor['drivers']
    if not (base['ocf_ttm'] and base['net_income_ttm'] and base['revenue_ttm'] and d['da_intensity']['value'] is not None):
        return None
    rev = base['revenue_ttm']['value']
    modelled = base['net_income_ttm']['value'] + rev * d['da_intensity']['value'] / 100
    actual = base['ocf_ttm']['value']
    return {'modelled_ocf_excluding_wc': modelled, 'actual_ocf': actual,
            'gap_percent': (actual / modelled - 1) * 100 if modelled else None}
