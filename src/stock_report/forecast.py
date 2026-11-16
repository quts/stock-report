"""Driver-based financial forecast for the next twelve months after the latest reported quarter.

Revenue → operating margin → operating income → non-operating items → pre-tax income → tax →
net income → diluted EPS. Cash forecasts require a reconciled statement bridge:
net income + D&A + other noncash adjustments + signed working-capital cash movements − capex.
Every driver carries its origin. Drivers the proposal leaves out fall back to the deterministic
historical anchor; anchors that cannot be measured are reported, never invented.
"""
from datetime import timedelta
from statistics import median
from .fin_series import ttm, year_ago

DRIVERS = ('revenue_growth', 'operating_margin', 'non_operating_ratio', 'tax_rate', 'share_change',
           'da_intensity', 'capex_intensity', 'working_capital_intensity', 'ocf_residual_intensity')
EXTREME_GROWTH = 30.0
ADDITIVE_METRICS = ('revenue', 'operating_income', 'pretax_income', 'net_income', 'depreciation_amortization',
                    'working_capital_investment', 'other_noncash_adjustments', 'operating_cash_flow',
                    'capital_expenditure', 'free_cash_flow', 'ebitda')


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
    proxy = balance_proxy(series, analysis)
    wc = ({'value': proxy['wc_proxy'], 'evidence_ids': proxy['evidence_ids']}
          if proxy.get('wc_proxy') is not None else None)
    prior_rev = None
    if rev:
        quarters_rev = series['quarters'].get('revenue', [])
        prior = [year_ago(quarters_rev, p) for p in quarters_rev[-4:]]
        if all(prior):
            prior_rev = sum(p['value'] for p in prior)
    if wc and rev and prior_rev and rev['value'] != prior_rev:
        put('working_capital_intensity', wc['value'] / (rev['value'] - prior_rev) * 100,
            '資產負債表營運資金餘額增加／營收增加：僅診斷，不用於現金流預測', wc['evidence_ids'] + rev['evidence_ids'])
    else:
        put('working_capital_intensity', None, '缺少可比的營運資金或前一年 TTM 營收')
    ranges = {'revenue_growth': (min(growth), max(growth)) if growth else None,
              'operating_margin': (min(margins), max(margins)) if margins else None}
    base = {'revenue_ttm': rev, 'operating_income_ttm': op, 'net_income_ttm': ni, 'pretax_ttm': pretax,
            'ocf_ttm': aligned(ttm(series, 'operating_cash_flow')), 'shares': shares}
    base['cashflow_reconciled'] = bool(rev and reconciliation['status'] == 'reconciled' and
                                      (reconciliation['start'], reconciliation['end'], reconciliation['currency']) ==
                                      (rev['start'], rev['end'], rev['currency']))
    base['cashflow_ratios'] = ({'other_noncash': reconciliation['noncash_adjustments'] / rev['value'],
                               'working_capital_cash_movement': reconciliation['working_capital_cash_movement'] / rev['value']}
                              if base['cashflow_reconciled'] and rev else None)
    put('ocf_residual_intensity', None,
        '未解釋殘差禁止外推；現金流量表無法完整勾稽時不產生 OCF／FCF 預測',
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
        windows = quarter_windows(series)
        start, end = (windows[0]['start'], windows[-1]['end']) if windows else (rev['end'] + timedelta(days=1), rev['end'] + timedelta(days=365))
        horizon = {'start': start.isoformat(), 'end': end.isoformat(), 'label': f'NTM {start.isoformat()}~{end.isoformat()}'}
    return {'drivers': out, 'ranges': ranges, 'base': base, 'notes': notes, 'horizon': horizon,
            'reconciliation': reconciliation, 'balance_sheet_proxy': proxy, 'series': series,
            'reliability': 'hypothetical_sensitivity'}


def quarter_windows(series):
    """Calendar quarters or an explicitly assumed 52-week fiscal calendar, never a 365-day stub."""
    from .fin_series import contiguous_tail
    past = contiguous_tail(series['quarters'].get('revenue', []), 4)
    if not past:
        return []
    weekly = all(((p['end'] - p['start']).days + 1) % 7 == 0 for p in past)
    def next_year(d):
        if weekly:
            return d + timedelta(weeks=52)
        try:
            return d.replace(year=d.year + 1)
        except ValueError:
            return d.replace(year=d.year + 1, day=28)
    first = past[-1]['end'] + timedelta(days=1)
    return [{'start': first + timedelta(days=91*i) if weekly else next_year(p['start']),
             'end': first + timedelta(days=91*(i+1)-1) if weekly else next_year(p['end']), 'prior': p,
             'calendar_basis': '52_week_model_assumption' if weekly else 'calendar_year'} for i,p in enumerate(past)]


def trend_drivers(anchor):
    """A level reference, not a claim that exceptional YoY growth will repeat."""
    series = anchor['series']
    drivers = {k: dict(v) for k,v in anchor['drivers'].items()}
    revs = series['quarters'].get('revenue', [])
    recent = revs[-2:]
    prior = [year_ago(revs, p) for p in recent]
    growth = 0.0 if recent else None
    ops = [next((p for p in series['quarters'].get('operating_income', [])
                 if (p['start'], p['end']) == (r['start'], r['end'])), None) for r in recent]
    margin = anchor['drivers']['operating_margin']['value']
    for key,value in (('revenue_growth', growth), ('operating_margin', margin)):
        drivers[key].update(value=value, basis='model_assumption',
                            derivation='營收以最新已報季度水準作無新增成長的參考路徑；年增率隨比較基期自然變化，不預設衰減係數。利潤率延用 TTM。此為機械情境，非已驗證預測。')
    drivers['revenue_growth']['policy'] = 'level_carry_forward'
    drivers['revenue_growth']['seasonality_status'] = 'not_separable_from_trend_in_short_history'
    drivers['revenue_growth']['latest_two_yoy_diagnostic'] = (
        median((r['value'] / p['value'] - 1) * 100 for r,p in zip(recent, prior))
        if len(recent) == 2 and all(p and p['value'] for p in prior) else None)
    return drivers


def quarterly_project(anchor, drivers, guidance=(), fx_rate=None, growth_policy=None):
    """Same-quarter prior-year seasonality, explicit YoY trend and period-specific guidance."""
    series, base = anchor['series'], anchor['base']
    windows = quarter_windows(series)
    if len(windows) != 4:
        return None, []
    rows, used, rejected = [], [], []
    prior_rev = base['revenue_ttm']
    policy = growth_policy or drivers['revenue_growth'].get('policy', 'explicit_yoy')
    if not prior_rev or (policy == 'explicit_yoy' and drivers['revenue_growth']['value'] is None):
        return None, []
    historical_margin = (base['operating_income_ttm']['value'] / prior_rev['value'] * 100
                         if base['operating_income_ttm'] else None)
    for w in windows:
        prior = w['prior']
        d = {k: dict(v) for k,v in drivers.items()}
        origin = {'method': 'same_quarter_yoy_trend', 'evidence_ids': prior['evidence_ids'],
                  'growth_assumption': d['revenue_growth']['value']}
        if policy == 'level_carry_forward':
            latest = series['quarters']['revenue'][-1]
            d['revenue_growth']['value'] = (latest['value'] / prior['value'] - 1) * 100
            origin = {'method': 'latest_reported_level_reference', 'evidence_ids': latest['evidence_ids'],
                      'anchor_period_end': latest['end'].isoformat(), 'anchor_revenue': latest['value'],
                      'comparison_period_end': prior['end'].isoformat(), 'comparison_revenue': prior['value'],
                      'growth_assumption': d['revenue_growth']['value'],
                      'limitation': '不外推例外季度的年增率；最新季水準本身也可能異常，延續該水準尚未被證實，須配合替代水準敏感度。比較基期追上造成的年增下降是算術效果，非需求衰退證據。短歷史未能分離季節性與趨勢。'}
        # Keep historical seasonal margins while shifting the annual margin by the scenario delta.
        op = next((p for p in series['quarters'].get('operating_income', [])
                   if (p['start'], p['end']) == (prior['start'], prior['end'])), None)
        if policy == 'explicit_yoy' and op and historical_margin is not None and prior['value'] and d['operating_margin']['value'] is not None:
            d['operating_margin']['value'] += op['value'] / prior['value'] * 100 - historical_margin
        applicable = []
        for g in guidance:
            if g['metric'] != 'revenue' or g['target_period_end'] != w['end'].isoformat():
                continue
            # The registered label must identify a quarter, not an annual number ending on the same day.
            if (not g.get('quarterly_period_verified') or g['bucket'] != 'reported' or
                    (g.get('declared_start') and g['declared_start'] != w['start'].isoformat())):
                rejected.append({'evidence_ids': g['evidence_ids'], 'reason': 'guidance_period_or_basis_unverified'})
                continue
            conversion = 1 if g['currency'] == prior['currency'] else fx_rate
            if not conversion or conversion <= 0:
                rejected.append({'evidence_ids': g['evidence_ids'], 'reason': 'missing_explicit_fx'})
                continue
            applicable.append((g, conversion))
        if applicable:
            latest_date = max(g['published'] for g,_ in applicable)
            latest = [(g,f) for g,f in applicable if g['published'] == latest_date]
            values = [(g['low'] + g['high']) / 2 * f for g,f in latest]
            if max(values) - min(values) > max(values) * 0.005:
                rejected.append({'reason': 'conflicting_guidance', 'evidence_ids': [i for g,_ in latest for i in g['evidence_ids']]})
            else:
                g,f = latest[0]
                d['revenue_growth']['value'] = (values[0] / prior['value'] - 1) * 100
                origin = {'method': 'management_guidance_midpoint', 'evidence_ids': g['evidence_ids'],
                          'guidance_period_end': g['target_period_end'], 'fx_rate': f,
                          'target_period_start': w['start'].isoformat()}
                used.append(origin)
        quarter_base = {**base, 'revenue_ttm': prior}
        s = project(quarter_base, d)
        if s is None:
            return None, rows
        rows.append({'start': w['start'], 'end': w['end'], 'calendar_basis': w['calendar_basis'],
                     'revenue_origin': origin, 'statements': s, 'growth_policy': policy,
                     'revenue_confidence': 'evidence_supported_forecast' if origin['method'] == 'management_guidance_midpoint' else 'mechanical_scenario',
                     'confidence_scope': 'revenue_input_only_not_whole_statement_or_statistical_validation',
                     'operating_margin_origin': {'method': 'explicit_yoy_seasonal_shift' if policy == 'explicit_yoy' else 'ttm_margin_reference',
                                                 'evidence_ids': drivers['operating_margin']['evidence_ids']}})
    annual = {key: sum(r['statements'][key] for r in rows) if all(r['statements'][key] is not None for r in rows) else None
              for key in ADDITIVE_METRICS}
    shares = [r['statements']['diluted_shares'] for r in rows]
    annual['diluted_shares'] = (sum(s * ((r['end'] - r['start']).days + 1) for s,r in zip(shares, rows)) /
                                sum((r['end'] - r['start']).days + 1 for r in rows)) if all(shares) else None
    annual['eps'] = annual['net_income'] / annual['diluted_shares'] if annual['net_income'] is not None and annual['diluted_shares'] else None
    annual['fcf_per_share'] = annual['free_cash_flow'] / annual['diluted_shares'] if annual['free_cash_flow'] is not None and annual['diluted_shares'] else None
    annual['revenue_growth'] = (annual['revenue'] / prior_rev['value'] - 1) * 100
    annual['operating_margin'] = annual['operating_income'] / annual['revenue'] * 100 if annual['revenue'] and annual['operating_income'] is not None else None
    annual['ocf_residual'] = None
    annual['guidance_used'], annual['guidance_rejected'] = used, rejected
    return annual, rows


def guidance_records(data, entity_id):
    from .expectations import forward_records, ranges
    evidence = forward_records(data, entity_id, ('management_guidance',))
    records = ranges(evidence)
    index = {e.evidence_id: e for e in evidence}
    import re
    for g in records:
        # A declared source start wins; otherwise accept an explicit quarterly source label only.
        g['quarterly_period_verified'] = all(e.period_basis in (None, 'single_quarter') and
            ((e.period_start and e.period_end and e.period_basis == 'single_quarter') or
             bool(re.search(r'Q[1-4]|第[一二三四1234]季', e.target_period or '')))
            for e in (index[i] for i in g['evidence_ids']))
        starts = {index[i].period_start.isoformat() for i in g['evidence_ids'] if index[i].period_start}
        g['declared_start'] = next(iter(starts)) if len(starts) == 1 else None
        if len(starts) > 1:
            g['quarterly_period_verified'] = False
        g['target_period_end'] = g['target_period_end'].isoformat()
    return records


def project(base, drivers):
    """Deterministic statements for one scenario; returns None for fields whose inputs are missing."""
    rev0, shares = base['revenue_ttm'], base['shares']
    d = {k: (v['value'] if v and v.get('value') is not None else None) for k, v in drivers.items()}
    if rev0 is None or d.get('revenue_growth') is None:
        return None
    revenue = rev0['value'] * (1 + d['revenue_growth'] / 100)
    op = revenue * d['operating_margin'] / 100 if d.get('operating_margin') is not None else None
    pretax = op + revenue * d['non_operating_ratio'] / 100 if op is not None and d.get('non_operating_ratio') is not None else None
    tax = d.get('tax_rate')
    ni = pretax * (1 - tax / 100) if tax is not None and pretax is not None else None
    share_count = shares['value'] * (1 + d['share_change'] / 100) if shares and d.get('share_change') is not None else None
    eps = ni / share_count if ni is not None and share_count else None
    da = revenue * d['da_intensity'] / 100 if d.get('da_intensity') is not None else None
    ratios = base.get('cashflow_ratios') if base.get('cashflow_reconciled') else None
    wc = -revenue * ratios['working_capital_cash_movement'] if ratios else None
    noncash = revenue * ratios['other_noncash'] if ratios else None
    residual = None  # An unexplained historical residual is never a forecast driver.
    ocf = ni + da - wc + noncash if all(v is not None for v in (ni, da, wc, noncash)) else None
    capex = revenue * d['capex_intensity'] / 100 if d.get('capex_intensity') is not None else None
    fcf = ocf - capex if ocf is not None and capex is not None else None
    return {'revenue': revenue, 'revenue_growth': d['revenue_growth'], 'operating_margin': d['operating_margin'],
            'operating_income': op, 'pretax_income': pretax, 'net_income': ni, 'diluted_shares': share_count,
            'eps': eps, 'depreciation_amortization': da, 'working_capital_investment': wc, 'operating_cash_flow': ocf,
            'capital_expenditure': capex, 'free_cash_flow': fcf, 'ocf_residual': residual, 'other_noncash_adjustments': noncash,
            'fcf_per_share': fcf / share_count if fcf is not None and share_count else None,
            'ebitda': op + da if da is not None and op is not None else None}


def reconcile(series, analysis=None):
    from .cashflow import reconcile as statement_bridge
    return statement_bridge(series)


def balance_proxy(series, analysis=None):
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
    from .backtesting import rolling_origin
    return rolling_origin(data, entity_id)


def calibration(anchor):
    """Legacy diagnostic field; not a fitted residual and never a reliability claim."""
    r = anchor['reconciliation']
    if r.get('residual') is None:
        return None
    return {'basis': 'cash_flow_statement_components', 'reconstructed_ocf': r['modelled_ocf'],
            'actual_ocf': r['actual_ocf'], 'unexplained_residual': r['residual'],
            'gap_percent': r['residual_percent_ocf'], 'not_forecast_validation': True}
