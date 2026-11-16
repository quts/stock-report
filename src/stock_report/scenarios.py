"""Bull / Base / Bear scenarios built from driver-based forecasts and referenced multiples.

The proposal chooses drivers (each with its origin) and a multiple with its basis; this module
computes the statements, EPS, cash flows and implied value. A multiple is evidence-based only
when it sits inside a sufficient historical or peer reference; otherwise the result is a
hypothetical valuation sensitivity, shown with reduced precision and never as a price target.
Probabilities are not accepted.
"""
from .forecast import DRIVERS, project, quarterly_project, guidance_records
from .policy import reportable
from .fin_series import ttm, build

BASIS_KINDS = {'historical_fact': ('fact', 'reported_claim'), 'management_guidance': ('management_guidance',),
               'guidance_informed_model': ('management_guidance',),
               'consensus': ('consensus_estimate', 'analyst_estimate')}
SENSITIVITY_METRIC = (0.9, 1.0, 1.1)
SENSITIVITY_MULTIPLE = (0.8, 1.0, 1.2)
METRIC_FOR = {'pe': 'eps', 'p_fcf': 'fcf_per_share', 'ev_ebitda': 'ebitda', 'p_b': 'book_value_per_share'}
CURRENT_METHOD = {'pe': 'pe_trailing', 'p_fcf': 'p_fcf', 'ev_ebitda': 'ev_ebitda', 'p_b': 'p_b'}
PEER_METRIC = {'pe': 'pe_trailing', 'ev_ebitda': 'ev_ebitda', 'p_fcf': 'p_fcf', 'p_b': 'p_b'}


def implied_price(method, metric_value, multiple, market):
    if metric_value is None:
        return None
    if method in ('pe', 'p_fcf', 'p_b'):
        return metric_value * multiple
    shares, net_debt = (market.get('shares') or {}).get('value'), market.get('net_debt')
    if not shares or net_debt is None:
        return None
    return (metric_value * multiple - net_debt) / shares


def round_sig(value, digits=2):
    if not value:
        return value
    from math import floor, log10
    return round(value, -int(floor(log10(abs(value)))) + digits - 1)


def reference_for(method, basis, references):
    if basis == 'historical_range' and method == 'pe':
        return references['historical_pe']
    if basis == 'peer_median':
        return references['peers'].get(PEER_METRIC[method])
    return None


def _basis_issues(a, index, request):
    cited = [index.get(i) for i in a.evidence_ids]
    if not cited:
        return [] if a.basis in ('historical_fact', 'model_assumption') else [f'假設「{a.name}」標為 {a.basis}，但沒有引用證據']
    if any(e is None or not reportable(e, request) for e in cited):
        return [f'假設「{a.name}」引用了不可用的證據']
    if a.basis != 'model_assumption' and any(e.statement_kind not in BASIS_KINDS[a.basis] for e in cited):
        return [f'假設「{a.name}」標為 {a.basis}，但引用的證據類別不同（指引不能當共識，反之亦然）']
    return []


def evaluate(data, proposal, valuation, entity, forecast_anchor=None, references=None):
    rows = [s for s in proposal.scenarios if s.entity_id == entity.entity_id] if proposal else []
    if not rows:
        return {'status': 'not_provided', 'scenarios': [], 'problems': [], 'sensitivity': None,
                'probabilities': 'not_assigned', 'valuation_basis': None}
    index = {e.evidence_id: e for e in data.evidence}
    references = references or {'historical_pe': {'status': 'insufficient'}, 'peers': {}}
    problems = []
    if sorted(s.name for s in rows) != ['base', 'bear', 'bull']:
        problems.append('情境必須恰好包含 bull、base、bear 各一個')
    available = {r['method']: r for r in valuation['results'] if r['status'] == 'available'}
    price = valuation['market']['price']
    reference_price = price.get('value') if price.get('status') == 'available' else None
    out = []
    for s in sorted(rows, key=lambda s: ('bear', 'base', 'bull').index(s.name)):
        issues = [i for a in list(s.assumptions) + list(s.drivers.values()) for i in _basis_issues(a, index, data.request)]
        for name, a in s.drivers.items():
            cited = [index[i] for i in a.evidence_ids if i in index]
            if a.forecast_period and a.forecast_period != s.target_period:
                issues.append(f'期間錯配：{name} 預測期間與情境不同')
            if not all((a.source_period, a.forecast_period, a.accounting_basis, a.derivation)):
                issues.append(f'輸入追溯：{name} 缺來源期間、預測期間、口徑或推導')
            if a.basis == 'historical_fact' and name in ('revenue_growth', 'operating_margin'):
                expected = {'revenue_growth': {'revenue'}, 'operating_margin': {'operating_income', 'revenue', 'operating_margin'}}[name]
                if cited and any(e.financial_metric not in expected for e in cited):
                    issues.append(f'輸入追溯：{name} 不能以訂單、積壓或不相符指標作為已實現營收／利益率')
            if a.basis in ('management_guidance', 'guidance_informed_model'):
                if not all((a.source_period, a.forecast_period, a.accounting_basis, a.derivation)):
                    issues.append(f'指引歸因：{name} 缺來源期間、預測期間、口徑或推導')
                periods = [e.target_period for e in cited if e.target_period]
                if a.source_period and periods and not any(p in a.source_period or a.source_period in p for p in periods):
                    issues.append(f'期間錯配：{name} 宣告的來源期間與引用指引不同')
                if a.basis == 'management_guidance':
                    exact = [e for e in cited if e.target_period == s.target_period and e.financial_metric == name
                             and e.value is not None and abs(e.value - a.value) < 1e-6]
                    if not exact:
                        issues.append(f'指引歸因：{name} 不是公司直接揭露的同期間同指標數字，應為 guidance_informed_model')
                if any(e.accounting_basis in ('non_gaap', 'company_adjusted') for e in cited) and a.accounting_basis in ('reported', 'gaap', 'ifrs'):
                    issues.append(f'口徑錯配：{name} 以調整後指引支持報表口徑')
        drivers, statements, quarter_rows = {}, None, []
        if forecast_anchor is not None:
            for name in DRIVERS:
                if name in s.drivers:
                    a = s.drivers[name]
                    drivers[name] = {**a.model_dump(), 'source': 'proposal'}
                else:
                    drivers[name] = {**forecast_anchor['drivers'][name], 'source': 'historical_anchor'}
                drv = drivers[name]
                drv['forecast_period'] = s.target_period
                drv['forecast_role'] = 'direct_guidance' if drv['basis'] == 'management_guidance' else 'model_assumption'
                if name in ('working_capital_intensity', 'ocf_residual_intensity'):
                    drv['application'] = 'diagnostic_only_not_used_in_cash_forecast'
                drv['evidence_role'] = 'context_only' if drv['basis'] == 'model_assumption' else 'derivation_input'
                drv['source_evidence'] = [
                    {'id': e.evidence_id, 'url': e.source_url, 'period': e.target_period or e.period,
                     'start': e.period_start, 'end': e.target_period_end or e.period_end,
                     'accounting_basis': e.accounting_basis or 'unspecified', 'metric': e.financial_metric, 'value': e.value}
                    for e in (index[i] for i in drv['evidence_ids'] if i in index)]
            if s.forecast_method == 'quarterly':
                guidance = guidance_records(data, entity.entity_id) if s.name == 'base' else []
                fx_ids = [index[i] for i in s.guidance_fx_evidence_ids if i in index and reportable(index[i], data.request)]
                fx_valid = s.guidance_fx_rate is not None and any(
                    e.value == s.guidance_fx_rate or str(s.guidance_fx_rate) in e.claim for e in fx_ids)
                if s.guidance_fx_rate and not fx_valid:
                    issues.append('指引歸因：換算匯率缺少核實來源，不使用跨幣別指引')
                statements, quarter_rows = quarterly_project(forecast_anchor, drivers, guidance,
                                                             s.guidance_fx_rate if fx_valid else None, s.growth_policy)
                if statements:
                    for key in ('revenue_growth', 'operating_margin'):
                        drivers[key]['input_value'] = drivers[key]['value']
                        drivers[key]['value'] = statements[key]
                        drivers[key]['aggregation'] = 'four_quarter_projection'
            else:
                statements = project(forecast_anchor['base'], drivers)
                if s.name == 'base':
                    issues.append('季度預測：Base 不得以單季年化敏感度代替四季預測')
            if statements is None:
                issues.append('缺少 TTM 營收、營業利益率或營收成長假設，無法建立財務預測')
        metric_name = METRIC_FOR[s.multiple_method]
        metric_value = statements.get(metric_name) if statements else None
        if metric_value is None and s.valuation_metric_value is not None:
            metric_value = s.valuation_metric_value
            issues.append('估值指標直接指定，未由營收、利潤率與現金流推導')
        elif s.valuation_metric_value is not None and metric_value and \
                abs(s.valuation_metric_value - metric_value) > 0.01 * abs(metric_value):
            issues.append(f'提案的 {metric_name} {s.valuation_metric_value} 與模型推導的 {metric_value:.2f} 不一致')
        ref = reference_for(s.multiple_method, s.multiple_basis, references)
        evidence_based = False
        if s.multiple_basis == 'current_trailing':
            current = available.get(CURRENT_METHOD[s.multiple_method])
            if current is None:
                issues.append(f'倍數基礎為目前 {s.multiple_method}，但該估值方法不可用')
            elif abs(s.multiple / current['value'] - 1) > 0.1:
                issues.append(f'倍數 {s.multiple} 標為目前倍數，但與目前 {current["value"]:.1f} 相差超過 10%')
        elif s.multiple_basis in ('historical_range', 'peer_median'):
            if ref is None or ref.get('status') != 'sufficient':
                issues.append(f'倍數基礎為 {s.multiple_basis}，但參考資料不足；應標為 model_assumption')
            elif not ref['stats']['min'] <= s.multiple <= ref['stats']['max']:
                issues.append(f'倍數 {s.multiple} 超出 {s.multiple_basis} 區間 {ref["stats"]["min"]:.1f}–{ref["stats"]["max"]:.1f}')
            else:
                evidence_based = True
        value = implied_price(s.multiple_method, metric_value, s.multiple, valuation['market'])
        if value is None and metric_value is not None:
            issues.append('缺少股數或淨負債，無法換算每股價值')
        upside = None if value is None or reference_price is None else (value / reference_price - 1) * 100
        problems.extend(f'{s.name}：{i}' for i in issues)
        invalidations = []
        for c in s.operating_invalidations:
            d = drivers.get(c.driver, {}).get('value')
            min_distance = 1.0 if c.driver == 'revenue_growth' else 0.25
            if (c.period != s.target_period or d is None or
                    (c.operator == 'lt' and c.threshold >= d) or (c.operator == 'gt' and c.threshold <= d) or
                    (d is not None and abs(c.threshold - d) < min_distance)):
                issues.append(f'失效條件不矛盾：{c.driver} 門檻未推翻同期間假設')
            invalidations.append(c.model_dump())
        # Legacy prose is retained for compatibility but cannot pass the economic gate.
        driver_labels = {'revenue_growth': '營收成長', 'operating_margin': '營業利益率', 'capex_intensity': '資本支出／營收',
                         'working_capital_intensity': '營運資金／營收增量'}
        invalidation_text = [f'{driver_labels.get(c.driver, c.driver)} {"低於" if c.operator == "lt" else "高於"} {c.threshold:.1f}%'
                             for c in s.operating_invalidations] or s.invalidation_conditions
        reverse = None
        if reference_price is not None and s.multiple_method in ('pe', 'p_fcf'):
            metric0 = None
            if forecast_anchor:
                if s.multiple_method == 'pe':
                    eps0 = ttm(build(data, entity.entity_id), 'diluted_eps')
                    metric0 = eps0['value'] if eps0 else None
                else:
                    ocf0 = forecast_anchor['base']['ocf_ttm']
                    capex0 = ttm(build(data, entity.entity_id), 'capital_expenditure')
                    shares0 = valuation['market'].get('shares') or forecast_anchor['base']['shares']
                    same_window = ocf0 and capex0 and (ocf0['start'], ocf0['end'], ocf0['currency']) == (capex0['start'], capex0['end'], capex0['currency'])
                    metric0 = (ocf0['value'] - capex0['value']) / shares0['value'] if same_window and shares0 and shares0['value'] > 0 else None
            required = reference_price / s.multiple
            reverse = {'metric': metric_name, 'multiple': s.multiple, 'required_per_share': required,
                       'ttm_per_share': metric0, 'required_growth_percent': (required / metric0 - 1) * 100 if metric0 and metric0 > 0 else None,
                       'interpretation': '固定股數、倍數之價格／倍數損益兩平敏感度；無風險折現或必要報酬，不證明倍數合理或構成目標價。'}
        problems.extend(f'{s.name}：{i}' for i in issues if f'{s.name}：{i}' not in problems)
        out.append({'name': s.name, 'target_period': s.target_period, 'financial_outcome': s.financial_outcome,
                    'forecast_method': s.forecast_method, 'quarterly_forecast': quarter_rows,
                    'forecast_reliability': 'hypothetical_sensitivity',
                    'confidence_class': 'mechanical_scenario', 'validation_status': 'not_validated',
                    'valuation_is_price_target': False, 'display_significant_digits': 2,
                    'assumptions': [a.model_dump() for a in s.assumptions], 'drivers': drivers, 'statements': statements,
                    'valuation': {'metric': metric_name, 'metric_value': metric_value, 'method': s.multiple_method,
                                  'multiple': s.multiple, 'multiple_basis': s.multiple_basis,
                                  'multiple_evidence_ids': s.multiple_evidence_ids,
                                  'reference': None if ref is None else {k: ref.get(k) for k in ('status', 'stats')}},
                    'evidence_based_multiple': evidence_based,
                    'implied_price': None if value is None else round(value, 4),
                    'implied_price_display': None if value is None else round_sig(value),
                    'reference_price': reference_price,
                    'change_vs_reference_percent': None if upside is None else round(upside, 2),
                    'catalysts': s.catalysts, 'invalidation_conditions': invalidation_text,
                    'operating_invalidations': invalidations, 'leading_indicators': s.leading_indicators,
                    'reverse_valuation': reverse,
                    'model_assumption_count': sum(d['basis'] in ('model_assumption', 'guidance_informed_model') for d in drivers.values())
                    + sum(a.basis == 'model_assumption' for a in s.assumptions), 'issues': issues})
    prices = {r['name']: r['implied_price'] for r in out if r['implied_price'] is not None}
    if len(prices) == 3 and not prices['bear'] <= prices['base'] <= prices['bull']:
        problems.append('情境隱含價值順序不合理：應為 bear ≤ base ≤ bull')
    base = next((r for r in out if r['name'] == 'base'), None)
    basis = 'evidence_based' if out and all(r['evidence_based_multiple'] for r in out) else 'hypothetical_sensitivity'
    sensitivity = None
    if base and base['implied_price'] is not None:
        v = base['valuation']
        shown = round_sig
        sensitivity = {'metric': v['metric'], 'method': v['method'], 'reference_price': reference_price,
                       'metric_factors': list(SENSITIVITY_METRIC), 'multiple_factors': list(SENSITIVITY_MULTIPLE),
                       'precision': 'two_significant_digits',
                       'grid': [[shown(implied_price(v['method'], v['metric_value'] * f, v['multiple'] * m,
                                                     valuation['market']) or 0)
                                 for m in SENSITIVITY_MULTIPLE] for f in SENSITIVITY_METRIC]}
    return {'status': 'invalid' if problems else 'available', 'scenarios': out, 'problems': problems,
            'sensitivity': sensitivity, 'probabilities': 'not_assigned', 'valuation_basis': basis,
            'forecast_confidence': 'mechanical_scenario',
            'confidence_classes': ['mechanical_scenario', 'evidence_supported_forecast', 'validated_forecast'],
            'confidence_note': 'Multiple references and arithmetic do not validate financial forecasts. Sample insufficiency lowers confidence, never removes computable earnings sensitivity. No validated forecast is asserted.'}
