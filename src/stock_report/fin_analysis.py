"""Deterministic financial analysis on the quarterly series.

Each metric is a derived calculation with its formula, window and input evidence. A metric that
cannot be computed carries a status and the reason, never a placeholder value.
"""
from .fin_series import contiguous_tail, latest_instant, ttm, year_ago
from .storage import read_json

HISTORY_QUARTERS = 8
EXTREME_GROWTH = 100.0       # percent YoY
EXTREME_SWING = 50.0         # percentage-point change in YoY growth between adjacent quarters


def sector_profile(ticker):
    import os
    config = read_json(os.environ.get('STOCK_REPORT_SECTOR_PROFILES', 'config/sector-profiles.json'))
    name = config['tickers'].get(ticker, 'general')
    return name, config['profiles'][name]


def metric(name, value, unit, *, status='available', window=None, formula=None, evidence_ids=(), note=None):
    return {'name': name, 'value': None if value is None else round(value, 6), 'unit': unit, 'status': status,
            'window': window, 'formula': formula, 'evidence_ids': sorted(set(evidence_ids)), 'note': note}


def missing(name, status, note):
    return metric(name, None, None, status=status, note=note)


def window_of(p):
    return f'{p["start"].isoformat() if p.get("start") else "?"}~{p["end"].isoformat()}'


def ratio(a, b):
    return None if a is None or b in (None, 0) else a / b


def _same_window(series, metric_name, point):
    for p in series['quarters'].get(metric_name, []):
        if p['start'] == point['start'] and p['end'] == point['end']:
            return p
    return None


def quarter_table(series):
    """Up to eight most recent single quarters with the core statement lines."""
    revenue = series['quarters'].get('revenue', [])
    rows = []
    for rev in revenue[-HISTORY_QUARTERS:]:
        row = {'start': rev['start'], 'end': rev['end'], 'revenue': rev, 'derived': rev['derived']}
        for name in ('gross_profit', 'operating_income', 'net_income', 'diluted_eps', 'operating_cash_flow',
                     'capital_expenditure'):
            row[name] = _same_window(series, name, rev)
        for name, num in (('gross_margin', 'gross_profit'), ('operating_margin', 'operating_income'),
                          ('net_margin', 'net_income')):
            row[name] = None if row[num] is None or rev['value'] <= 0 else row[num]['value'] / rev['value'] * 100
        if row['gross_margin'] is None:
            reported = _same_window(series, 'gross_margin', rev)
            row['gross_margin'] = reported['value'] if reported else None
        ago = year_ago(revenue, rev)
        row['revenue_yoy'] = None if ago is None or ago['value'] <= 0 else (rev['value'] / ago['value'] - 1) * 100
        rows.append(row)
    return rows


def analyse(data, series, entity_id):
    profile_name, profile = sector_profile(entity_id)
    out, flags = [], []
    rows = quarter_table(series)
    revenue = series['quarters'].get('revenue', [])
    history = len(contiguous_tail(revenue, min(len(revenue), HISTORY_QUARTERS)) or []) if revenue else 0
    out.append(metric('history_quarters', history, 'quarters', status='available' if history >= HISTORY_QUARTERS else
                      ('available' if history else 'not_found'),
                      note=None if history >= HISTORY_QUARTERS else f'只有 {history} 季連續單季營收，少於 {HISTORY_QUARTERS} 季'))
    # Growth and acceleration
    if rows and rows[-1]['revenue_yoy'] is not None:
        last = rows[-1]
        ago = year_ago(revenue, last['revenue'])
        out.append(metric('revenue_growth_yoy', last['revenue_yoy'], 'percent', window=window_of(last['revenue']),
                          formula='(quarter revenue / same quarter a year earlier - 1) * 100',
                          evidence_ids=last['revenue']['evidence_ids'] + ago['evidence_ids']))
        prior = rows[-2]['revenue_yoy'] if len(rows) > 1 else None
        if prior is not None:
            accel = last['revenue_yoy'] - prior
            out.append(metric('growth_acceleration', accel, 'percentage_points', window=window_of(last['revenue']),
                              formula='latest YoY growth - previous quarter YoY growth',
                              evidence_ids=last['revenue']['evidence_ids']))
            if abs(accel) > EXTREME_SWING:
                flags.append('營收年增率在相鄰兩季變動超過 50 個百分點，成長波動大，趨勢外推需保守')
        if last['revenue_yoy'] > EXTREME_GROWTH:
            flags.append('營收年增率超過 100%，基期效應或一次性因素可能扭曲比較')
    else:
        out.append(missing('revenue_growth_yoy', 'not_found', '沒有可比的去年同季單季營收'))
    # Margins and trend
    for name in ('gross_margin', 'operating_margin', 'net_margin'):
        points = [(r['end'], r[name]) for r in rows if r[name] is not None]
        if not points:
            out.append(missing(name, 'not_found', f'缺少同期營收與{name}分子'))
            continue
        latest = points[-1][1]
        change = latest - points[-5][1] if len(points) >= 5 else None
        out.append(metric(name, latest, 'percent', window=window_of(rows[-1]['revenue']),
                          formula='same-window numerator / revenue * 100', evidence_ids=rows[-1]['revenue']['evidence_ids'],
                          note=None if change is None else f'較四季前 {change:+.1f} 個百分點'))
        if change is not None:
            out.append(metric(f'{name}_change_4q', change, 'percentage_points', window=window_of(rows[-1]['revenue']),
                              formula='latest margin - margin four quarters earlier'))
    # EPS
    eps = series['quarters'].get('diluted_eps', [])
    if eps:
        ago = year_ago(eps, eps[-1])
        if ago and ago['value'] > 0:
            out.append(metric('eps_growth_yoy', (eps[-1]['value'] / ago['value'] - 1) * 100, 'percent',
                              window=window_of(eps[-1]), formula='(diluted EPS / year-ago diluted EPS - 1) * 100',
                              evidence_ids=eps[-1]['evidence_ids'] + ago['evidence_ids']))
        elif ago:
            out.append(missing('eps_growth_yoy', 'not_applicable', '去年同季 EPS 為零或負，成長率無意義'))
        if eps[-1]['value'] < 0:
            flags.append('最新一季稀釋 EPS 為負，本益比類估值不適用')
    ttm_eps = ttm(series, 'diluted_eps')
    ttm_ni, shares = ttm(series, 'net_income'), series['quarters'].get('diluted_shares', [])
    if ttm_eps:
        out.append(metric('eps_ttm', ttm_eps['value'], 'per_share', window=window_of(ttm_eps), formula=ttm_eps['formula'],
                          evidence_ids=ttm_eps['evidence_ids'],
                          note='四季稀釋 EPS 加總；各季股數不同，與 TTM 淨利除以期末股數可能有差異'))
    elif ttm_ni and shares and shares[-1]['end'] == ttm_ni['end']:
        out.append(metric('eps_ttm', ttm_ni['value'] / shares[-1]['value'], 'per_share', window=window_of(ttm_ni),
                          formula='TTM net income / latest quarter weighted diluted shares',
                          evidence_ids=ttm_ni['evidence_ids'] + shares[-1]['evidence_ids'],
                          note='缺少連續四季單季 EPS，以 TTM 淨利除以最新一季加權平均稀釋股數估算'))
    else:
        out.append(missing('eps_ttm', 'not_found', '沒有連續四季的單季稀釋 EPS，也無法以 TTM 淨利與股數推算'))
    # Cash conversion: TTM first, otherwise the latest matching cumulative window.
    cash = cash_window(series)
    if cash is None:
        out.append(missing('operating_cash_flow_to_net_income', 'not_found', '沒有與淨利同一期間的營業現金流'))
        out.append(missing('free_cash_flow', 'not_found', '缺少同一期間的營業現金流與資本支出'))
    else:
        label, ocf, ni, capex, rev = cash
        ids = [i for p in (ocf, ni, capex, rev) if p for i in p['evidence_ids']]
        q = ratio(ocf['value'], ni['value']) if ni else None
        out.append(metric('operating_cash_flow_to_net_income', q, 'ratio', window=f'{label} {window_of(ocf)}',
                          formula='operating cash flow / net income (same window)', evidence_ids=ids,
                          status='available' if q is not None else 'not_found'))
        if q is not None and ni['value'] > 0 and q < 0.8:
            flags.append('營業現金流低於淨利的八成，盈餘品質需看營運資金與會計認列時點')
        if capex:
            fcf = ocf['value'] - capex['value']
            out.append(metric('free_cash_flow', fcf, ocf['unit'], window=f'{label} {window_of(ocf)}',
                              formula='operating cash flow - capital expenditure (same window)', evidence_ids=ids))
            out.append(metric('fcf_conversion', ratio(fcf, ni['value']) if ni and ni['value'] > 0 else None, 'ratio',
                              window=f'{label} {window_of(ocf)}', formula='free cash flow / net income', evidence_ids=ids,
                              status='available' if ni and ni['value'] > 0 else 'not_applicable'))
            if rev:
                out.append(metric('capex_intensity', capex['value'] / rev['value'] * 100, 'percent',
                                  window=f'{label} {window_of(ocf)}', formula='capital expenditure / revenue * 100',
                                  evidence_ids=ids))
            if fcf < 0:
                flags.append('自由現金流為負，P/FCF 不適用，資金缺口需看融資來源')
        else:
            out.append(missing('free_cash_flow', 'not_found', '缺少與營業現金流同期間的資本支出'))
    # Working capital (receivables + inventory - payables) versus about a year earlier.
    wc = working_capital(series)
    out.extend(wc)
    # Liquidity and leverage
    out.extend(leverage(series))
    out.append(missing('roic', 'not_applicable' if profile_name == 'financials' else 'not_found',
                       '需要有效稅率與投入資本的完整拆分；本版不以假設稅率估算 ROIC'))
    return {'profile': profile_name, 'profile_label': profile['label'], 'metrics': out, 'flags': flags,
            'quarters': rows, 'interpretation_focus': profile['interpretation'], 'key_metrics': profile['key_metrics'],
            'conflicts': series['conflicts']}


def cash_window(series):
    names = ('operating_cash_flow', 'net_income', 'capital_expenditure', 'revenue')
    ttms = {n: ttm(series, n) for n in names}
    ocf, ni = ttms['operating_cash_flow'], ttms['net_income']
    if ocf and ni and ocf['end'] == ni['end']:
        capex = ttms['capital_expenditure'] if ttms['capital_expenditure'] and ttms['capital_expenditure']['end'] == ocf['end'] else None
        rev = ttms['revenue'] if ttms['revenue'] and ttms['revenue']['end'] == ocf['end'] else None
        trailing = ('TTM', ocf, ni, capex, rev)
        if capex:
            return trailing
    else:
        trailing = None
    cumulative = _cumulative_window(series)
    if cumulative and (cumulative[3] or trailing is None):
        return cumulative
    return trailing


def _cumulative_window(series):
    for ocf in reversed(series['cumulative'].get('operating_cash_flow', [])):
        def match(name):
            return next((p for p in series['cumulative'].get(name, [])
                         if p['start'] == ocf['start'] and p['end'] == ocf['end']), None)
        ni = match('net_income')
        if ni is None:
            continue
        return ('YTD', ocf, ni, match('capital_expenditure'), match('revenue'))
    return None


def working_capital(series):
    parts = {}
    for name in ('accounts_receivable_net', 'inventory', 'accounts_payable'):
        alt = 'accounts_receivable' if name == 'accounts_receivable_net' else None
        parts[name] = series['instants'].get(name) or (series['instants'].get(alt) if alt else None)
    if not parts['accounts_receivable_net'] or not parts['inventory']:
        return [missing('working_capital_change', 'not_found', '缺少應收帳款或存貨的資產負債表時點')]
    latest_end = min(parts['accounts_receivable_net'][-1]['end'], parts['inventory'][-1]['end'])

    def at(name, end):
        rows = [p for p in (parts[name] or []) if p['end'] == end]
        return rows[0] if rows else None

    def level(end):
        ar, inv = at('accounts_receivable_net', end), at('inventory', end)
        if ar is None or inv is None:
            return None
        ap = at('accounts_payable', end)
        return ar['value'] + inv['value'] - (ap['value'] if ap else 0), [ar, inv] + ([ap] if ap else []), ap is not None
    now = level(latest_end)
    candidates = sorted({p['end'] for p in parts['inventory'] if 300 <= (latest_end - p['end']).days <= 380}
                        | {p['end'] for p in parts['inventory'] if 120 <= (latest_end - p['end']).days < 300})
    earlier = next((level(end) for end in candidates if level(end)), None)
    out = []
    if now and earlier:
        ids = [i for p in now[1] + earlier[1] for i in p['evidence_ids']]
        out.append(metric('working_capital_change', now[0] - earlier[0], now[1][0]['unit'],
                          window=f'{earlier[1][0]["end"]}→{latest_end}',
                          formula='(AR + inventory - AP) latest - earlier balance date', evidence_ids=ids,
                          note=None if now[2] else '未取得應付帳款，僅計應收與存貨'))
        for name, label in (('inventory', 'inventory_growth'), ('accounts_receivable_net', 'receivables_growth')):
            a, b = at(name, latest_end), at(name, earlier[1][0]['end'])
            if a and b and b['value'] > 0:
                out.append(metric(label, (a['value'] / b['value'] - 1) * 100, 'percent',
                                  window=f'{b["end"]}→{a["end"]}', formula='balance / earlier balance - 1',
                                  evidence_ids=a['evidence_ids'] + b['evidence_ids']))
    else:
        out.append(missing('working_capital_change', 'not_found', '沒有可比較的較早資產負債表時點'))
    return out


def debt_at(series, end=None):
    names = ('short_term_debt', 'long_term_debt', 'bonds_payable', 'total_debt')
    latest = {n: latest_instant(series, n, end) for n in names}
    if latest['total_debt']:
        return latest['total_debt']['value'], latest['total_debt']['end'], latest['total_debt']['evidence_ids']
    parts = [p for n, p in latest.items() if p and n != 'total_debt']
    if not parts:
        return None
    end_date = max(p['end'] for p in parts)
    parts = [p for p in parts if p['end'] == end_date]
    return sum(p['value'] for p in parts), end_date, [i for p in parts for i in p['evidence_ids']]


def leverage(series):
    cash = latest_instant(series, 'cash_equivalents')
    debt = debt_at(series)
    out = []
    if cash is None or debt is None or cash['end'] != debt[1]:
        out.append(missing('net_debt', 'not_found', '缺少同一資產負債表日的現金與有息負債'))
        return out
    net = debt[0] - cash['value']
    out.append(metric('net_debt', net, cash['unit'], window=f'@{cash["end"]}', formula='interest-bearing debt - cash',
                      evidence_ids=debt[2] + cash['evidence_ids'],
                      note='有息負債為設定的負債科目加總；租賃負債未計入'))
    op, da = ttm(series, 'operating_income'), ttm(series, 'depreciation_amortization')
    if op and da and op['end'] == da['end']:
        ebitda = op['value'] + da['value']
        out.append(metric('ebitda_ttm', ebitda, op['unit'], window=window_of(op), formula='TTM operating income + TTM D&A',
                          evidence_ids=op['evidence_ids'] + da['evidence_ids']))
        if ebitda > 0:
            out.append(metric('net_debt_to_ebitda', net / ebitda, 'ratio', window=window_of(op),
                              formula='net debt / TTM EBITDA', evidence_ids=op['evidence_ids'] + da['evidence_ids']))
    else:
        out.append(missing('ebitda_ttm', 'not_found', '缺少連續四季營業利益或折舊攤銷'))
    return out
