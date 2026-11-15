"""Bull / Base / Bear scenarios computed from explicit, labelled assumptions.

The model proposes assumptions; this module validates their basis and computes the implied
price, the change versus the reference price and a sensitivity grid. Probabilities are not
accepted: a scenario is a conditional outcome, not a forecast with a weight.
"""
from .policy import reportable

BASIS_KINDS = {'historical_fact': ('fact', 'reported_claim'), 'management_guidance': ('management_guidance',),
               'consensus': ('consensus_estimate', 'analyst_estimate')}
SENSITIVITY_METRIC = (0.9, 1.0, 1.1)
SENSITIVITY_MULTIPLE = (0.8, 1.0, 1.2)


def implied_price(method, metric_value, multiple, market):
    if method in ('pe', 'p_fcf', 'p_b'):
        return metric_value * multiple
    shares, net_debt = (market.get('shares') or {}).get('value'), market.get('net_debt')
    if not shares or net_debt is None:
        return None
    return (metric_value * multiple - net_debt) / shares


def evaluate(data, proposal, valuation, entity):
    rows = [s for s in proposal.scenarios if s.entity_id == entity.entity_id] if proposal else []
    if not rows:
        return {'status': 'not_provided', 'scenarios': [], 'problems': [], 'sensitivity': None}
    index = {e.evidence_id: e for e in data.evidence}
    problems = []
    if sorted(s.name for s in rows) != ['base', 'bear', 'bull']:
        problems.append('情境必須恰好包含 bull、base、bear 各一個')
    available = {r['method']: r for r in valuation['results'] if r['status'] == 'available'}
    method_map = {'pe': 'pe_trailing', 'p_fcf': 'p_fcf', 'p_b': 'p_b', 'ev_ebitda': 'ev_ebitda'}
    price = valuation['market']['price']
    reference = price.get('value') if price.get('status') == 'available' else None
    out = []
    for s in sorted(rows, key=lambda s: ('bear', 'base', 'bull').index(s.name)):
        issues = []
        for a in s.assumptions:
            if a.basis == 'model_assumption':
                continue
            cited = [index.get(i) for i in a.evidence_ids]
            if not cited or any(e is None or not reportable(e, data.request) for e in cited):
                issues.append(f'假設「{a.name}」標為 {a.basis}，但沒有可用的引用證據')
            elif any(e.statement_kind not in BASIS_KINDS[a.basis] for e in cited):
                issues.append(f'假設「{a.name}」標為 {a.basis}，但引用的證據類別不同（指引不能當共識，反之亦然）')
        if s.multiple_basis == 'current_trailing':
            current = available.get(method_map[s.multiple_method])
            if current is None:
                issues.append(f'倍數基礎為目前 {s.multiple_method}，但該估值方法不可用')
        elif s.multiple_basis in ('historical_range', 'peer_median'):
            key = 'historical_range' if s.multiple_basis == 'historical_range' else 'peer_relative'
            if key not in available:
                issues.append(f'倍數基礎為 {s.multiple_basis}，但沒有可用的{key}資料；應標為 model_assumption')
        price_value = implied_price(s.multiple_method, s.valuation_metric_value, s.multiple, valuation['market'])
        if price_value is None:
            issues.append('EV/EBITDA 情境缺少股數或淨負債，無法換算每股價值')
        upside = None if price_value is None or reference is None else (price_value / reference - 1) * 100
        problems.extend(f'{s.name}：{i}' for i in issues)
        out.append({'name': s.name, 'target_period': s.target_period, 'financial_outcome': s.financial_outcome,
                    'assumptions': [a.model_dump() for a in s.assumptions],
                    'valuation': {'metric': s.valuation_metric, 'metric_value': s.valuation_metric_value,
                                  'method': s.multiple_method, 'multiple': s.multiple, 'multiple_basis': s.multiple_basis,
                                  'multiple_evidence_ids': s.multiple_evidence_ids},
                    'implied_price': None if price_value is None else round(price_value, 4),
                    'reference_price': reference, 'change_vs_reference_percent': None if upside is None else round(upside, 2),
                    'catalysts': s.catalysts, 'invalidation_conditions': s.invalidation_conditions,
                    'model_assumption_count': sum(a.basis == 'model_assumption' for a in s.assumptions),
                    'issues': issues})
    prices = {r['name']: r['implied_price'] for r in out if r['implied_price'] is not None}
    if len(prices) == 3 and not prices['bear'] <= prices['base'] <= prices['bull']:
        problems.append('情境隱含股價順序不合理：應為 bear ≤ base ≤ bull')
    base = next((r for r in out if r['name'] == 'base'), None)
    sensitivity = None
    if base and base['implied_price'] is not None:
        v = base['valuation']
        sensitivity = {'metric': v['metric'], 'method': v['method'], 'reference_price': reference,
                       'metric_factors': list(SENSITIVITY_METRIC), 'multiple_factors': list(SENSITIVITY_MULTIPLE),
                       'grid': [[round(implied_price(v['method'], v['metric_value'] * f, v['multiple'] * m,
                                                     valuation['market']) or 0, 2)
                                 for m in SENSITIVITY_MULTIPLE] for f in SENSITIVITY_METRIC]}
    return {'status': 'invalid' if problems else 'available', 'scenarios': out, 'problems': problems,
            'sensitivity': sensitivity, 'probabilities': 'not_assigned'}
