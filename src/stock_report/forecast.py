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
           'da_intensity', 'capex_intensity', 'working_capital_intensity', 'ocf_residual_intensity')
EXTREME_GROWTH = 30.0


def _pct(a, b):
    return None if a is None or not b else a / b * 100


def anchors(series, analysis):
    rev, op, ni = ttm(series, 'revenue'), ttm(series, 'operating_income'), ttm(series, 'net_income')
    pretax, da, capex = ttm(series, 'income_before_tax'), ttm(series, 'depreciation_amortization'), ttm(series, 'capital_expenditure')
    def aligned(p):
        return p if p and rev and (p['start'], p['end'], p['currency']) == (rev['start'], rev['end'], rev['currency']) else None
    op, ni, pretax, da, capex = map(aligned, (op, ni, pretax, da, capex))
    shares = (series['quarters'].get('diluted_shares') or [None])[-1]
    if shares and rev and shares['end'] != rev['end']:
        shares = None
    quarters = analysis['quarters']
    growth = [q['revenue_yoy'] for q in quarters if q['revenue_yoy'] is not None]
    margins = [q['operating_margin'] for q in quarters if q['operating_margin'] is not None]
    out, notes = {}, []

    def put(name, value, rationale, ids=(), status='available'):
        out[name] = {'value': None if value is None else round(value, 4), 'basis': 'historical_fact',
                     'rationale': rationale, 'evidence_ids': sorted(set(ids)), 'status': status if value is not None else 'not_found'}
    recent = growth[-4:]
    g = median(recent) if recent else None
    growth_ids = [i for p in series['quarters'].get('revenue', [])[-8:] for i in p['evidence_ids']]
    put('revenue_growth', g, f'最近 {len(recent)} 季營收年增率中位數', growth_ids)
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
    reconciliation = reconcile(series, analysis)
    wc = ({'value': reconciliation['wc_proxy'], 'evidence_ids': reconciliation['evidence_ids']}
          if reconciliation.get('wc_proxy') is not None else None)
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
            'ocf_ttm': aligned(ttm(series, 'operating_cash_flow')), 'shares': shares}
    residual = reconciliation.get('residual')
    put('ocf_residual_intensity', _pct(residual, rev['value']) if rev and residual is not None else None,
        'TTM 未解釋 OCF 殘差／營收；含未取得非現金、稅款、融資應收及其他營運資金，不是已核實的單一科目',
        reconciliation.get('evidence_ids', []))
    out['ocf_residual_intensity']['basis'] = 'model_assumption'
    out['share_change']['basis'] = 'model_assumption'
    for name, driver in out.items():
        driver['derivation'] = driver['rationale']
        driver['source_period'] = f'{rev["start"]}~{rev["end"]}' if rev else None
        driver['accounting_basis'] = 'reported'
        if name == 'share_change' and shares:
            driver['source_period'] = f'{shares["start"]}~{shares["end"]}'
    horizon = None
    if rev:
        start, end = rev['end'] + timedelta(days=1), rev['end'] + timedelta(days=365)
        horizon = {'start': start.isoformat(), 'end': end.isoformat(), 'label': f'NTM {start.isoformat()}~{end.isoformat()}'}
    return {'drivers': out, 'ranges': ranges, 'base': base, 'notes': notes, 'horizon': horizon,
            'reconciliation': reconciliation}


def project(base, drivers):
    """Deterministic statements for one scenario; returns None for fields whose inputs are missing."""
    rev0, shares = base['revenue_ttm'], base['shares']
    d = {k: (v['value'] if v and v.get('value') is not None else None) for k, v in drivers.items()}
    if rev0 is None or d.get('revenue_growth') is None or d.get('operating_margin') is None:
        return None
    revenue = rev0['value'] * (1 + d['revenue_growth'] / 100)
    op = revenue * d['operating_margin'] / 100
    pretax = op + revenue * d['non_operating_ratio'] / 100 if d.get('non_operating_ratio') is not None else None
    tax = d.get('tax_rate')
    ni = pretax * (1 - tax / 100) if tax is not None and pretax is not None else None
    share_count = shares['value'] * (1 + d['share_change'] / 100) if shares and d.get('share_change') is not None else None
    eps = ni / share_count if ni is not None and share_count else None
    da = revenue * d['da_intensity'] / 100 if d.get('da_intensity') is not None else None
    wc = (revenue - rev0['value']) * d['working_capital_intensity'] / 100 if d.get('working_capital_intensity') is not None else None
    residual = revenue * d['ocf_residual_intensity'] / 100 if d.get('ocf_residual_intensity') is not None else None
    ocf = ni + da - wc + residual if all(v is not None for v in (ni, da, wc, residual)) else None
    capex = revenue * d['capex_intensity'] / 100 if d.get('capex_intensity') is not None else None
    fcf = ocf - capex if ocf is not None and capex is not None else None
    return {'revenue': revenue, 'revenue_growth': d['revenue_growth'], 'operating_margin': d['operating_margin'],
            'operating_income': op, 'pretax_income': pretax, 'net_income': ni, 'diluted_shares': share_count,
            'eps': eps, 'depreciation_amortization': da, 'working_capital_investment': wc, 'operating_cash_flow': ocf,
            'capital_expenditure': capex, 'free_cash_flow': fcf, 'ocf_residual': residual,
            'fcf_per_share': fcf / share_count if fcf is not None and share_count else None,
            'ebitda': op + da if da is not None else None}


def reconcile(series, analysis):
    """Balance-sheet WC is a proxy, never a cash-flow-statement reconciliation item."""
    points = {m: ttm(series, m) for m in ('revenue', 'net_income', 'depreciation_amortization', 'operating_cash_flow')}
    ref = points['revenue']
    if ref is None or any(p is None or (p['start'], p['end'], p['currency']) !=
                          (ref['start'], ref['end'], ref['currency']) for p in points.values()):
        return {'status': 'unavailable', 'residual': None, 'reason': '缺少同期間、同幣別 OCF／淨利／折舊攤銷'}
    # Require AR, inventory and AP at both endpoints. Acquisitions/FX/noncash movements remain residual.
    components, ids = {}, [i for p in points.values() for i in p['evidence_ids']]
    for name, sign in (('accounts_receivable_net', 1), ('inventory', 1), ('accounts_payable', -1)):
        rows = series['instants'].get(name, [])
        current = next((p for p in rows if p['end'] == ref['end']), None)
        prior = next((p for p in reversed(rows) if abs((ref['start'] - p['end']).days - 1) <= 7), None)
        if not current or not prior or current['currency'] != ref['currency'] or prior['currency'] != ref['currency']:
            return {'status': 'unavailable', 'residual': None, 'reason': f'缺同期間 {name} 期初／期末餘額'}
        components[name] = sign * (current['value'] - prior['value'])
        ids.extend(current['evidence_ids'] + prior['evidence_ids'])
    wc = sum(components.values())
    ni, da, actual = (points[m]['value'] for m in ('net_income', 'depreciation_amortization', 'operating_cash_flow'))
    model = ni + da - wc
    return {'status': 'partial', 'start': ref['start'], 'end': ref['end'], 'net_income': ni, 'da': da,
            'wc_components': components, 'wc_proxy': wc, 'modelled_ocf': model, 'actual_ocf': actual,
            'residual': actual - model, 'residual_percent_ocf': (actual - model) / abs(actual) * 100 if actual else None,
            'evidence_ids': sorted(set(ids)),
            'limitation': '僅淨利、折舊攤銷及應收／存貨／應付餘額變化；其他非現金、遞延稅、融資應收等缺逐項證據，殘差未解釋。延用殘差比率是模型假設。'}


def backtest(data, entity_id):
    """Expanding historical vintages, publication-time filtered; never fit to target-period OCF."""
    from . import fin_series, fin_analysis
    full = fin_series.build(data, entity_id)
    revs = full['quarters'].get('revenue', [])
    rows = []
    for origin in revs[3:-4]:
        relevant = [e for e in data.evidence if e.entity_id == entity_id and e.financial_metric == 'revenue'
                    and e.period_end == origin['end'] and e.statement_kind == 'fact']
        if not relevant:
            continue
        # Comparative facts reappear in later filings. Use the earliest available publication,
        # never the latest repeat (which could reveal the entire forecast target).
        vintage = min(e.published_at for e in relevant)
        timely = vintage.date() <= origin['end'] + timedelta(days=120)
        observed = data.model_copy(update={'evidence': [e for e in data.evidence if (not timely or e.published_at <= vintage)
                                                       and (e.period_end is None or e.period_end <= origin['end'])]})
        train = fin_series.build(observed, entity_id)
        a = anchors(train, fin_analysis.analyse(observed, train, entity_id))
        if not a['base']['revenue_ttm'] or a['base']['revenue_ttm']['end'] != origin['end']:
            continue
        pred = project(a['base'], a['drivers'])
        target = [r for r in revs if r['start'] > origin['end']][:4]
        if len(target) != 4 or pred is None:
            continue
        end = target[-1]['end']
        future = {key: {m: [p for p in pts if p['end'] <= end] for m, pts in full[key].items()}
                  for key in ('quarters', 'instants', 'cumulative')}
        actual = {m: ttm(future, m) for m in ('revenue', 'net_income', 'operating_cash_flow', 'capital_expenditure')}
        errors = {}
        for name in ('revenue', 'net_income', 'operating_cash_flow', 'free_cash_flow'):
            if name == 'free_cash_flow':
                ocf, capex = actual['operating_cash_flow'], actual['capital_expenditure']
                value = ocf['value'] - capex['value'] if ocf and capex and ocf['start'] == capex['start'] else None
            else:
                p = actual[name]
                value = p['value'] if p and p['start'] == target[0]['start'] else None
            estimate = pred.get(name)
            errors[name] = {'predicted': estimate, 'actual': value,
                            'error_percent': (estimate - value) / abs(value) * 100 if estimate is not None and value else None}
        if timely and vintage.date() >= end:
            continue
        rows.append({'origin': origin['end'], 'information_cutoff': vintage.isoformat() if timely else None,
                     'vintage_status': 'publication_filtered' if timely else 'current_vintage_period_holdout',
                     'dataset_cutoff': data.request.research_as_of.isoformat(), 'target_end': end,
                     'errors': errors})
    n = sum(r['errors']['free_cash_flow']['error_percent'] is not None for r in rows)
    return {'status': 'limited' if n else 'insufficient', 'fcf_test_count': n, 'rows': rows,
            'limitation': '四季留出、訓練財務期間不含預測期；無原始公布版本時僅做目前版本的回溯測試，可能含重編，不是即時交易回測。樣本少且可能重疊，不能證明 FCF 預測可靠。'}


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
