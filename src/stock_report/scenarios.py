"""Bull / Base / Bear scenarios built from driver-based forecasts and referenced multiples.

The proposal chooses drivers (each with its origin) and a multiple with its basis; this module
computes the statements, EPS, cash flows and implied value. A multiple is evidence-based only
when it sits inside a sufficient historical or peer reference; otherwise the result is a
hypothetical valuation sensitivity, shown with reduced precision and never as a price target.
Probabilities are not accepted.
"""
from .forecast import DRIVERS, project
from .policy import reportable

BASIS_KINDS = {'historical_fact': ('fact', 'reported_claim'), 'management_guidance': ('management_guidance',),
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
    if a.basis == 'model_assumption':
        return []
    cited = [index.get(i) for i in a.evidence_ids]
    if not cited:
        return [] if a.basis == 'historical_fact' else [f'假設「{a.name}」標為 {a.basis}，但沒有引用證據']
    if any(e is None or not reportable(e, request) for e in cited):
        return [f'假設「{a.name}」引用了不可用的證據']
    if any(e.statement_kind not in BASIS_KINDS[a.basis] for e in cited):
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
        drivers, statements = {}, None
        if forecast_anchor is not None:
            for name in DRIVERS:
                if name in s.drivers:
                    a = s.drivers[name]
                    drivers[name] = {'value': a.value, 'basis': a.basis, 'evidence_ids': a.evidence_ids,
                                     'rationale': a.rationale, 'source': 'proposal'}
                else:
                    drivers[name] = {**forecast_anchor['drivers'][name], 'source': 'historical_anchor'}
            statements = project(forecast_anchor['base'], drivers)
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
        out.append({'name': s.name, 'target_period': s.target_period, 'financial_outcome': s.financial_outcome,
                    'assumptions': [a.model_dump() for a in s.assumptions], 'drivers': drivers, 'statements': statements,
                    'valuation': {'metric': metric_name, 'metric_value': metric_value, 'method': s.multiple_method,
                                  'multiple': s.multiple, 'multiple_basis': s.multiple_basis,
                                  'multiple_evidence_ids': s.multiple_evidence_ids,
                                  'reference': None if ref is None else {k: ref.get(k) for k in ('status', 'stats')}},
                    'evidence_based_multiple': evidence_based,
                    'implied_price': None if value is None else round(value, 4),
                    'implied_price_display': None if value is None else (round(value, 0) if evidence_based else round_sig(value)),
                    'reference_price': reference_price,
                    'change_vs_reference_percent': None if upside is None else round(upside, 2),
                    'catalysts': s.catalysts, 'invalidation_conditions': s.invalidation_conditions,
                    'model_assumption_count': sum(d['basis'] == 'model_assumption' for d in drivers.values())
                    + sum(a.basis == 'model_assumption' for a in s.assumptions), 'issues': issues})
    prices = {r['name']: r['implied_price'] for r in out if r['implied_price'] is not None}
    if len(prices) == 3 and not prices['bear'] <= prices['base'] <= prices['bull']:
        problems.append('情境隱含價值順序不合理：應為 bear ≤ base ≤ bull')
    base = next((r for r in out if r['name'] == 'base'), None)
    basis = 'evidence_based' if out and all(r['evidence_based_multiple'] for r in out) else 'hypothetical_sensitivity'
    sensitivity = None
    if base and base['implied_price'] is not None:
        v = base['valuation']
        shown = (lambda x: round(x, 2)) if basis == 'evidence_based' else round_sig
        sensitivity = {'metric': v['metric'], 'method': v['method'], 'reference_price': reference_price,
                       'metric_factors': list(SENSITIVITY_METRIC), 'multiple_factors': list(SENSITIVITY_MULTIPLE),
                       'precision': 'exact' if basis == 'evidence_based' else 'two_significant_digits',
                       'grid': [[shown(implied_price(v['method'], v['metric_value'] * f, v['multiple'] * m,
                                                     valuation['market']) or 0)
                                 for m in SENSITIVITY_MULTIPLE] for f in SENSITIVITY_METRIC]}
    return {'status': 'invalid' if problems else 'available', 'scenarios': out, 'problems': problems,
            'sensitivity': sensitivity, 'probabilities': 'not_assigned', 'valuation_basis': basis}
