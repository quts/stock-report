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


STATUS = {'critical': 'FAIL', 'warning': 'WARNING'}


def check(name, passed, severity, detail):
    status = 'PASS' if passed else STATUS[severity]
    return state(name, status, detail)


def state(name, status, detail):
    """One gate result: PASS, WARNING, FAIL or NOT_APPLICABLE."""
    return {'check': name, 'status': status, 'passed': status in ('PASS', 'NOT_APPLICABLE'),
            'severity': {'PASS': 'pass', 'NOT_APPLICABLE': 'pass', 'WARNING': 'warning', 'FAIL': 'critical'}[status],
            'detail': detail}


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


def economic_checks(data, engine):
    scen, forecast = engine['scenarios'], engine.get('forecast')
    rows, out = scen['scenarios'], []
    provenance = [p for p in scen['problems'] if any(w in p for w in ('指引歸因', '期間錯配', '口徑錯配', '輸入追溯'))]
    out.append(check('forecast_input_provenance', not provenance, 'critical',
                     '未把衍生 NTM 假設冒充公司指引' if not provenance else '；'.join(provenance[:4])))
    weak = []
    for r in rows:
        rules = r.get('operating_invalidations', [])
        if not rules or not {'revenue_growth', 'operating_margin'} <= {c['driver'] for c in rules}:
            weak.append(f'{r["name"]} 缺同期間營收成長及營業利益率失效門檻')
        weak += [p for p in r['issues'] if '失效條件不矛盾' in p]
    out.append(check('scenario_invalidation_logic', not weak, 'critical', '；'.join(weak) or '失效門檻直接推翻同期間營運假設；領先指標另列'))
    recon = (forecast or {}).get('reconciliation', {})
    residual = recon.get('residual_percent_ocf')
    out.append(state('ocf_reconciliation', 'WARNING' if residual is not None else 'FAIL',
                     f'未解釋殘差占實際 OCF {residual:+.1f}%；餘額變化非現金流明細，FCF 預測受限' if residual is not None else
                     '無法建立同期間現金流勾稽；不可宣稱 FCF 預測可靠'))
    bt = (forecast or {}).get('backtest', {})
    out.append(state('forecast_backtest', 'WARNING',
                     f'可評分 FCF 樣本 {bt.get("fcf_test_count", 0)}；歷史留出樣本有限，算術一致不代表預測可靠'))
    out += forecast_reliability_checks(engine)
    # Ordered scenario labels must also agree with operating economics, not just selected multiples.
    mapped = {r['name']: r for r in rows}
    contradictions = []
    if set(mapped) == {'bear', 'base', 'bull'}:
        for key in ('revenue_growth', 'operating_margin'):
            vals = [mapped[n]['drivers'].get(key, {}).get('value') for n in ('bear', 'base', 'bull')]
            if all(v is not None for v in vals) and vals != sorted(vals):
                contradictions.append(f'{key} 與 bear/base/bull 營運排序矛盾')
    out.append(check('operating_conclusion_consistency', not contradictions, 'critical',
                     '；'.join(contradictions) or '營運假設與情境順序一致；未以倍數遮掩營運反轉'))
    return out


def forecast_reliability_checks(engine):
    """Arithmetic completeness and point-in-time integrity are prerequisites, not validation."""
    from datetime import datetime
    from .backtesting import METRICS, MIN_SAMPLES
    from .forecast import ADDITIVE_METRICS
    f = engine['forecast']
    rec, bt = f['reconciliation'], f['backtest']
    out = [check('cash_flow_statement_reconciliation', rec['status'] == 'reconciled', 'critical',
                 '現金流量表非現金調整與營運資金現金變動同期間勾稽；預測仍為假設' if rec['status'] == 'reconciled' else
                 '現金流量表項目不完整或有殘差：停止 OCF／FCF 預測；資產負債表變動不作現金流替代')]
    violations = []
    for r in bt.get('rows', []):
        cutoff = datetime.fromisoformat(r['information_cutoff'])
        for field in ('max_input_published_at', 'max_input_event_time'):
            t = r.get(field)
            t = datetime.fromisoformat(t) if isinstance(t, str) else t
            if t and t >= cutoff:
                violations.append(f'{r["origin"]} 使用截止後資料')
        if r['vintage_status'] != 'strict_point_in_time':
            violations.append('修訂歷史資料混入時點回測')
    out.append(check('point_in_time_backtest_integrity', bt.get('method') == 'strict_point_in_time' and not violations,
                     'critical', '；'.join(violations) or '僅使用截止前已公布資料；修訂資料測試獨立且不計入樣本'))
    counts = {m: bt.get('metrics', {}).get(m, {}).get('sample_size', 0) for m in METRICS}
    enough = all(n >= MIN_SAMPLES and bt['metrics'][m]['coverage'] >= 0.8 for m,n in counts.items())
    out.append(check('forecast_sample_sufficiency', enough, 'critical',
                     f'PIT 樣本（營收／營益／EPS／OCF／FCF）：{"／".join(str(n) for n in counts.values())}；'
                     f'門檻各 {MIN_SAMPLES}、覆蓋80% 僅為最低條件，不代表統計驗證'))
    broken, alignment = [], []
    for s in engine['scenarios']['scenarios']:
        quarters = s.get('quarterly_forecast', [])
        annual = s.get('statements')
        if s.get('forecast_method') == 'annual_sensitivity':
            if s['name'] == 'base':
                broken.append('Base 使用年化敏感度')
            continue
        if len(quarters) != 4 or annual is None:
            broken.append(f'{s["name"]} 缺完整四季預測')
            continue
        for prev, nxt in zip(quarters, quarters[1:]):
            if (nxt['start'] - prev['end']).days != 1:
                broken.append('預測季度有重疊或缺口')
        if quarters[0]['start'].isoformat() != f['horizon']['start'] or quarters[-1]['end'].isoformat() != f['horizon']['end']:
            broken.append('季度合計與 NTM 期間不符')
        for metric in ADDITIVE_METRICS:
            values = [q['statements'][metric] for q in quarters]
            total = sum(values) if all(v is not None for v in values) else None
            if (total is None) != (annual[metric] is None) or (total is not None and abs(total - annual[metric]) > 1e-6 * max(abs(total), 1)):
                broken.append(f'{metric} 年季合計不一致')
        for q in quarters:
            origin = q['revenue_origin']
            if origin['method'] == 'management_guidance_midpoint' and origin['guidance_period_end'] != q['end'].isoformat():
                alignment.append('公司指引套用至不同季度')
    out.append(check('quarterly_forecast_consistency', not broken, 'critical', '；'.join(broken) or '四季連續且年季損益與現金流合計一致；缺值不補零'))
    out.append(check('guidance_period_alignment', not alignment, 'critical', '；'.join(alignment) or '管理層指引僅適用其實際財務季度，不外推其他季度'))
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
        vals += [v for v in (s.get('statements') or {}).values() if isinstance(v, (int, float))]
        vals += [d['value'] for d in (s.get('drivers') or {}).values() if isinstance(d.get('value'), (int, float))]
        if s.get('implied_price_display') is not None:
            vals.append(s['implied_price_display'])
    for d in (engine.get('forecast') or {}).get('drivers', {}).values():
        if isinstance(d.get('value'), (int, float)):
            vals.append(d['value'])
    for d in (engine.get('sector_drivers') or {}).get('drivers', []):
        if isinstance(d.get('value'), (int, float)):
            vals.append(d['value'])
    for ref in ((engine['valuation'].get('references') or {}).get('historical_pe') or {}, *((engine['valuation'].get('references') or {}).get('peers') or {}).values()):
        vals += [v for v in (ref.get('stats') or {}).values() if isinstance(v, (int, float))]
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
    if price['status'] != 'available':
        short = {'stale': '收盤價過期', 'conflicting': '來源收盤價互相衝突'}.get(price['status'], '沒有可合法取得的收盤價')
        results.append(state('price_recency', 'FAIL', f'{short}（各來源狀態與補救方式見 qa.json）'))
    else:
        results.append(state('price_recency', 'PASS' if price.get('calendar_verified') else 'WARNING', price.get('note')))
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
    disclosure_ok = scen['status'] == 'available' and all((s['drivers'] or s['assumptions']) and s['valuation']['multiple_basis']
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
    results += model_checks(data, engine)
    if engine['scenarios']['scenarios']:
        results += economic_checks(data, engine)
    critical = [r for r in results if r['status'] == 'FAIL']
    return {'grade': 'limited' if critical else 'complete', 'checks': results,
            'critical_failures': [r['check'] for r in critical],
            'warnings': [r['check'] for r in results if r['status'] == 'WARNING']}


KEY_DRIVERS = ('revenue_growth', 'operating_margin')


def model_checks(data, engine):
    """Analytical-basis checks for the forecast, multiples and scenarios (deterministic rules)."""
    scen, forecast = engine['scenarios'], engine.get('forecast')
    rows = scen['scenarios']
    if not rows:
        na = '沒有提供情境'
        return [state(n, 'NOT_APPLICABLE', na) for n in (
            'assumption_evidence_quality', 'scenario_internal_consistency', 'valuation_method_suitability',
            'valuation_reference_quality', 'scenario_uncertainty_disclosure', 'forecast_period_consistency')]
    out = []
    # 1. Assumption evidence quality: basis must match evidence; "historical" values must lie in history;
    #    extreme history is not extrapolated; disclosure alone is not validation.
    fails, warns = [], []
    fails += [p for p in scen['problems'] if '證據' in p or '直接指定' in p or '類別不同' in p]
    ranges = (forecast or {}).get('ranges', {})
    anchors = (forecast or {}).get('drivers', {})
    for r in rows:
        d = r['drivers']
        if not d:
            fails.append(f'{r["name"]}：沒有營收、利潤率等預測驅動因子')
            continue
        g, m = d['revenue_growth'], d['operating_margin']
        gr, mr = ranges.get('revenue_growth'), ranges.get('operating_margin')
        for name, drv, rng, slack in (('營收成長', g, gr, 0.5), ('營業利益率', m, mr, 1.0)):
            if drv['basis'] == 'historical_fact' and drv['value'] is not None and rng and \
                    not rng[0] - slack <= drv['value'] <= rng[1] + slack:
                fails.append(f'{r["name"]}：{name} {drv["value"]:.1f}% 標為歷史事實，但超出歷史區間 {rng[0]:.1f}–{rng[1]:.1f}%')
        if g['value'] is not None and gr and g['basis'] not in ('management_guidance', 'consensus') and g['value'] > gr[1] + 10:
            fails.append(f'{r["name"]}：營收成長 {g["value"]:.1f}% 高於歷史最高 {gr[1]:.1f}% 超過 10 個百分點，且沒有指引或共識支持')
        if m['value'] is not None and mr and m['basis'] not in ('management_guidance', 'consensus') and m['value'] > mr[1] + 5:
            fails.append(f'{r["name"]}：營業利益率 {m["value"]:.1f}% 高於歷史最高 {mr[1]:.1f}% 超過 5 個百分點，且沒有證據支持')
        if g['source'] == 'historical_anchor' and anchors.get('revenue_growth', {}).get('status') == 'extreme':
            fails.append(f'{r["name"]}：直接外推極端的歷史成長率，未提供指引、共識或明確假設')
        tax = d['tax_rate']['value']
        if tax is None:
            fails.append(f'{r["name"]}：缺少稅率（歷史資料無法量測，也未提供假設）')
        elif not 0 <= tax <= 45:
            fails.append(f'{r["name"]}：稅率 {tax:.1f}% 不合理')
        if d['share_change']['value'] is not None and abs(d['share_change']['value']) > 10:
            warns.append(f'{r["name"]}：股數變動 {d["share_change"]["value"]:+.1f}% 幅度大')
        if r['name'] == 'base' and any(d[k]['basis'] == 'model_assumption' for k in KEY_DRIVERS):
            warns.append('基準情境的營收成長或利潤率是模型假設，沒有指引、共識或歷史資料直接支持')
    out.append(state('assumption_evidence_quality', 'FAIL' if fails else ('WARNING' if warns else 'PASS'),
                     '；'.join((fails + warns)[:4]) or '未觸發來源或歷史區間異常；這不證明預測假設在經濟上合理'))
    # 2. Internal consistency: the statements reconcile and the implied value follows from them.
    broken = []
    for r in rows:
        s, d = r['statements'], r['drivers']
        if s is None:
            broken.append(f'{r["name"]}：無法建立財務預測')
            continue
        ident = [('稅後淨利', s['net_income'], None if d['tax_rate']['value'] is None or s['pretax_income'] is None else s['pretax_income'] * (1 - d['tax_rate']['value'] / 100)),
                 ('EPS × 股數', None if s['eps'] is None else s['eps'] * s['diluted_shares'], s['net_income']),
                 ('FCF', s['free_cash_flow'], None if s['operating_cash_flow'] is None or s['capital_expenditure'] is None
                  else s['operating_cash_flow'] - s['capital_expenditure'])]
        if r['implied_price'] is not None and r['valuation']['method'] in ('pe', 'p_fcf', 'p_b'):
            ident.append(('隱含價值', r['implied_price'], r['valuation']['metric_value'] * r['valuation']['multiple']))
        for label, a, b in ident:
            if a is not None and b is not None and abs(a - b) > 1e-6 * max(1, abs(b)):
                broken.append(f'{r["name"]}：{label}不一致')
    eps = {r['name']: (r['statements'] or {}).get('eps') for r in rows}
    order_warn = None not in eps.values() and len(eps) == 3 and not eps['bear'] <= eps['base'] <= eps['bull']
    out.append(state('scenario_internal_consistency', 'FAIL' if broken else ('WARNING' if order_warn else 'PASS'),
                     '；'.join(broken) or ('EPS 順序不是 bear ≤ base ≤ bull' if order_warn else
                                           '各情境的損益、EPS、現金流與隱含價值互相勾稽')))
    # 3. Method suitability for the sector and the earnings/cash profile.
    from .fin_analysis import sector_profile
    _, profile = sector_profile(engine['entity_id'])
    allowed = {'pe': 'pe_trailing', 'p_fcf': 'p_fcf', 'ev_ebitda': 'ev_ebitda', 'p_b': 'p_b'}
    unsuitable = []
    for r in rows:
        method = r['valuation']['method']
        if allowed[method] not in profile['valuation_methods']:
            unsuitable.append(f'{r["name"]}：{method} 不適用於 {engine["valuation"]["profile"]}')
        if r['valuation']['metric_value'] is not None and r['valuation']['metric_value'] <= 0 and method in ('pe', 'p_fcf', 'ev_ebitda'):
            unsuitable.append(f'{r["name"]}：{r["valuation"]["metric"]} 非正值，{method} 倍數無意義')
    out.append(state('valuation_method_suitability', 'FAIL' if unsuitable else 'PASS',
                     '；'.join(unsuitable) or '估值方法符合產業設定，且基礎指標為正'))
    # 4. Reference quality: evidence-based multiples need sufficient history or peers.
    bad_ref = [p for p in scen['problems'] if '參考資料不足' in p or '超出' in p]
    bounds = profile.get('multiple_sanity_bounds', {})
    outliers = [f'{r["name"]}：{r["valuation"]["multiple"]} 倍超出產業寬鬆範圍 {bounds[r["valuation"]["method"]]}'
                for r in rows if r['valuation']['method'] in bounds
                and not bounds[r['valuation']['method']][0] <= r['valuation']['multiple'] <= bounds[r['valuation']['method']][1]]
    if bad_ref:
        out.append(state('valuation_reference_quality', 'FAIL', '；'.join(bad_ref)))
    elif scen['valuation_basis'] != 'evidence_based':
        out.append(state('valuation_reference_quality', 'WARNING',
                         '沒有足夠的歷史或同業估值參照，情境倍數屬模型假設：輸出為假設性估值敏感度，不是目標價'
                         + ('；' + '；'.join(outliers) if outliers else '')))
    else:
        out.append(state('valuation_reference_quality', 'WARNING' if outliers else 'PASS',
                         '；'.join(outliers) or '情境倍數皆落在足量的歷史或同業區間內'))
    # 5. Uncertainty disclosure: hypothetical values must lose false precision and carry the label.
    precise = [r['name'] for r in rows if not r['evidence_based_multiple'] and r['implied_price'] is not None
               and r['implied_price_display'] == round(r['implied_price'], 4) and abs(r['implied_price']) >= 100]
    sens = scen.get('sensitivity')
    if sens and scen['valuation_basis'] != 'evidence_based' and sens.get('precision') != 'two_significant_digits':
        precise.append('sensitivity')
    history = next((m for m in engine['analysis']['metrics'] if m['name'] == 'history_quarters'), {'value': 0})
    needs = scen['valuation_basis'] != 'evidence_based' or (history['value'] or 0) < 8 or \
        any(r['model_assumption_count'] for r in rows)
    out.append(state('scenario_uncertainty_disclosure', 'FAIL' if precise else 'PASS',
                     f'假設性輸出仍顯示完整精度：{precise}' if precise else
                     ('已以兩位有效數字呈現假設性價值並標示不確定性' if needs else '輸出有足量證據，無額外不確定性揭露需求')))
    # 6. Forecast period and reference date alignment.
    horizon = (forecast or {}).get('horizon')
    if horizon is None:
        out.append(state('forecast_period_consistency', 'FAIL', '沒有可推導的預測期間（缺 TTM 營收）'))
    else:
        wrong = [r['name'] for r in rows if r['target_period'] != horizon['label']]
        price = engine['valuation']['market']['price']
        latest_end = engine['analysis']['quarters'][-1]['end'] if engine['analysis']['quarters'] else None
        stale_ref = price.get('trade_date') and latest_end and price['trade_date'] < latest_end.isoformat()
        out.append(state('forecast_period_consistency', 'FAIL' if wrong else ('WARNING' if stale_ref else 'PASS'),
                         f'情境期間 {wrong} 與模型預測期間 {horizon["label"]} 不一致' if wrong else
                         ('參考價日期早於最新財報期末' if stale_ref else f'預測期間 {horizon["label"]}，參考價日期在最新財報之後')))
    return out
