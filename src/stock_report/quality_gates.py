"""Research-quality gates that run before any investment report is rendered.

A critical failure never blocks the user from seeing what is known; it changes the report grade
to `limited`, and the report says so on its first page. Checks are deterministic.
"""
import re
from .policy import reportable

NUMBER = re.compile(r'(?<![A-Za-z0-9_.,])[-+]?\d[\d,]*(?:\.\d+)?')
CAUSAL = re.compile(r'因為|由於|導致|帶動|所以|使得|because|driven by|due to|led to', re.IGNORECASE)
VALUATION_JUDGEMENT = re.compile(r'低估|高估|便宜|昂貴|undervalued|overvalued|cheap|expensive', re.IGNORECASE)
SCALES = (1, 1e-3, 1e-6, 1e-8, 1e-9, 1e-12, 100)
TOLERANCE = 0.006


def check(name, passed, severity, detail):
    return {'check': name, 'passed': bool(passed), 'severity': 'pass' if passed else severity, 'detail': detail}


DATE = re.compile(r'\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}/\d{1,2}|\d{4}年\d{1,2}月(?:\d{1,2}日)?|\d{1,2}月\d{1,2}日')


def numbers_in(text):
    out = []
    for raw in NUMBER.findall(DATE.sub(' ', text)):
        token = raw.replace(',', '')
        try:
            value = float(token)
        except ValueError:
            continue
        if re.fullmatch(r'(19|20)\d\d', token) or (value.is_integer() and abs(value) < 13 and '.' not in token):
            continue  # years, small counts, months and quarter numbers are not financial claims
        out.append((raw, abs(value)))
    return out


def supported(value, candidates):
    for c in candidates:
        if c == 0:
            continue
        for scale in SCALES:
            scaled = abs(c) * scale
            if scaled and abs(value - scaled) <= max(TOLERANCE * scaled, 0.051 if scaled < 100 else 0.5):
                return True
    return False


def candidates_for(ids, index, engine_numbers):
    values = list(engine_numbers)
    cited = [index[i] for i in ids if i in index]
    for e in cited:
        if e.value is not None:
            values.append(e.value)
        values.extend(v for _, v in numbers_in(e.claim))
    nums = [e.value for e in cited if e.value]
    for a in nums:
        for b in nums:
            if a is not b and b:
                values.append(a / b)
                values.append((a / b - 1))
            values.append(a - b)
    return values


def engine_values(engine):
    vals = []
    for m in engine['analysis']['metrics']:
        if m['value'] is not None:
            vals.append(m['value'])
    for r in engine['valuation']['results']:
        if r['value'] is not None:
            vals.append(r['value'])
            vals.extend(i['value'] for i in r['inputs'] if isinstance(i['value'], (int, float)))
    for s in engine['scenarios']['scenarios']:
        vals += [x for x in (s['implied_price'], s['change_vs_reference_percent'], s['reference_price'],
                             s['valuation']['metric_value'], s['valuation']['multiple']) if x is not None]
        vals += [a['value'] for a in s['assumptions']]
    for row in engine['analysis']['quarters']:
        vals += [v for k, v in row.items() if isinstance(v, (int, float))]
    market = engine['valuation']['market']
    vals += [v for v in (market.get('market_cap'), market.get('enterprise_value'), market.get('net_debt')) if v]
    return vals


def paragraphs(proposal, entity_id):
    out = []
    for section in proposal.company_research if proposal else []:
        if section.entity_id == entity_id:
            for field in ('overview', 'key_takeaways', 'fundamentals', 'news', 'earnings_preview', 'monitoring'):
                out += [(field, p.text, p.evidence_ids) for p in getattr(section, field)]
    for t in proposal.thesis if proposal else []:
        if t.entity_id == entity_id:
            for p in [t.market_pricing, *t.key_assumptions, *t.supporting, *t.opposing]:
                out.append(('thesis', p.text, p.evidence_ids))
    for o in proposal.outlooks if proposal else []:
        if o.entity_id == entity_id:
            out.append(('outlook', o.interpretation, o.supporting_evidence_ids + o.counter_evidence_ids))
    return out


def run(data, proposal, engine, entity, decision=None):
    index = {e.evidence_id: e for e in data.evidence}
    results = []
    price = engine['valuation']['market']['price']
    results.append(check('price_recency', price['status'] == 'available', 'critical',
                         price.get('note') if price['status'] != 'available' else
                         f'收盤日 {price["trade_date"]}，距截止 {price["trading_days_lag"]} 個交易日'))
    units = {}
    for name, points in engine['series']['quarters'].items():
        units.setdefault(name, set()).update((p['unit'], p['currency']) for p in points)
    mixed = [n for n, u in units.items() if len(u) > 1]
    results.append(check('currency_unit_consistency', not mixed, 'critical',
                         '各序列單位與幣別一致' if not mixed else f'序列混用單位／幣別：{mixed}'))
    conflicts = engine['series']['conflicts']
    results.append(check('conflicting_data', not conflicts, 'warning',
                         '沒有同期間數值衝突' if not conflicts else f'{len(conflicts)} 個期間的來源數值衝突，已排除'))
    gaps = [q for q in engine['analysis']['quarters'] if q['derived']]
    results.append(check('fiscal_period_consistency', True, 'warning',
                         f'單季序列連續性已檢查；{len(gaps)} 季由累計數推導並標示'))
    cited = [i for _, _, ids in paragraphs(proposal, entity.entity_id) for i in ids]
    missing = sorted({i for i in cited if i not in index or not reportable(index[i], data.request)})
    untraced = [r['method'] for r in engine['valuation']['results'] if r['status'] == 'available'
                and any(not i['evidence_ids'] for i in r['inputs'])]
    results.append(check('source_traceability', not missing and not untraced, 'critical',
                         '所有引用與估值輸入可回溯' if not missing and not untraced else
                         f'無法回溯：{missing[:5]} {untraced}'))
    bad_calc = []
    for r in engine['valuation']['results']:
        if r['status'] == 'available' and r['method'] == 'pe_trailing':
            p, e = r['inputs'][0]['value'], r['inputs'][1]['value']  # recompute from disclosed inputs
            if abs(p / e - r['value']) > 1e-6 * max(1, r['value']):
                bad_calc.append(r['method'])
    results.append(check('calculation_correctness', not bad_calc, 'critical',
                         '估值重算一致' if not bad_calc else f'重算不一致：{bad_calc}'))
    adjusted_mix = []
    for s in engine['scenarios']['scenarios']:
        for a in s['assumptions']:
            kinds = {index[i].accounting_basis for i in a['evidence_ids'] if i in index}
            if kinds & {'non_gaap', 'company_adjusted'} and s['valuation']['multiple_basis'] == 'current_trailing':
                adjusted_mix.append(s['name'])
    results.append(check('gaap_non_gaap_compatibility', not adjusted_mix, 'critical',
                         '沒有混用調整後與報表口徑' if not adjusted_mix else
                         f'情境以調整後數字乘上報表口徑倍數：{sorted(set(adjusted_mix))}'))
    mislabel = [p for p in engine['scenarios']['problems'] if '類別不同' in p]
    consensus_words = [t for f, t, ids in paragraphs(proposal, entity.entity_id)
                       if re.search(r'共識|consensus', t, re.I) and not re.search(r'沒有|未取得|不是|無', t)
                       and ids and all(i in index and index[i].statement_kind == 'management_guidance' for i in ids)]
    results.append(check('guidance_consensus_labeling', not mislabel and not consensus_words, 'critical',
                         '指引與共識分開標示' if not mislabel and not consensus_words else
                         f'指引被當成共識：{(mislabel + consensus_words)[:3]}'))
    gaps_mandatory = engine['completeness']['mandatory_gaps']
    results.append(check('mandatory_fields', not gaps_mandatory, 'critical',
                         '必要欄位齊全' if not gaps_mandatory else '缺少必要欄位：' + '、'.join(gaps_mandatory)))
    scen = engine['scenarios']
    disclosure_ok = scen['status'] == 'available' and all(s['assumptions'] and s['valuation']['multiple_basis']
                                                          for s in scen['scenarios'])
    results.append(check('valuation_assumption_disclosure', disclosure_ok, 'critical',
                         '每個情境都有明確假設、方法與倍數基礎' if disclosure_ok else
                         ('沒有提供情境' if scen['status'] == 'not_provided' else '；'.join(scen['problems'][:4]))))
    values = engine_values(engine)
    unsupported = []
    for field, text, ids in paragraphs(proposal, entity.entity_id):
        cands = candidates_for(ids, index, values)
        for raw, value in numbers_in(text):
            if not supported(value, cands):
                unsupported.append(f'{field}：{raw}')
    results.append(check('unsupported_numbers', len(unsupported) <= 3, 'critical',
                         '正文數字皆可回溯到證據或計算' if not unsupported else
                         f'{len(unsupported)} 個數字找不到對應證據或計算：{unsupported[:6]}'))
    causal = [text[:40] for _, text, ids in paragraphs(proposal, entity.entity_id) if CAUSAL.search(text) and len(set(ids)) < 2]
    results.append(check('unsupported_causal_claims', not causal, 'warning',
                         '因果敘述皆引用兩項以上證據' if not causal else f'因果敘述只引用單一證據：{causal[:3]}'))
    contradictions = []
    base = next((s for s in scen['scenarios'] if s['name'] == 'base'), None)
    for o in (decision.outlooks if decision else []):
        if o.entity_id != entity.entity_id or base is None or base['change_vs_reference_percent'] is None:
            continue
        if o.outlook == 'bullish' and base['change_vs_reference_percent'] < 0:
            contradictions.append(f'{o.horizon} 偏多但基準情境低於參考價')
        if o.outlook == 'bearish' and base['change_vs_reference_percent'] > 0:
            contradictions.append(f'{o.horizon} 偏空但基準情境高於參考價')
    if engine['valuation']['assessment'] != 'evaluated':
        judged = [text[:40] for _, text, _ in paragraphs(proposal, entity.entity_id) if VALUATION_JUDGEMENT.search(text)
                  and not re.search(r'無法|不能|未|沒有', text)]
        contradictions += [f'估值缺少歷史或同業參照卻判斷高低：{t}' for t in judged]
    results.append(check('section_contradictions', not contradictions, 'critical',
                         '各章節結論一致' if not contradictions else '；'.join(contradictions[:3])))
    critical = [r for r in results if r['severity'] == 'critical']
    return {'grade': 'limited' if critical else 'complete', 'checks': results,
            'critical_failures': [r['check'] for r in critical],
            'warnings': [r['check'] for r in results if r['severity'] == 'warning']}
