"""Reproduce reviewed reports using existing evidence, without new network data or permissions.

Two phases keep the operating contract explicit:

* `--prepare` creates immutable runs from the latest reviewed baseline inputs and records the
  hashes of research_input.json, context.json, proposal.schema.json and analysis-instructions.txt
  that the analyst reads before any proposal is written.
* `--finalize` writes the candidate-path proposals (no new facts; every number comes from the
  engine or cited evidence), finalizes, checks the actual PDF/research/manifest and emits version
  attribution next to the PDF.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from stock_report import engine
from stock_report.forecast_attribution import change_report
from stock_report.models import Proposal, Request, ResearchInput
from stock_report.pipeline import prepare, finalize
from stock_report.storage import read_json, write_json
from pypdf import PdfReader

SOURCES = [Path('reports/tsmc-2330-20261115/forecast-normalized-20261116/runs/abf01038ff114ab0bef3a985d4a5d302'),
           Path('reports/dell-v2-20261115/forecast-normalized-20261116/runs/a4f21b15dda6452e8f77c2058163a859')]
OUTPUT = 'candidate-paths-reviewed-20261116'
MAX_PAGES = 7  # The prior final reports were 7 pages; no expansion is accepted.
CONTRACT = ('research_input.json', 'context.json', 'proposal.schema.json', 'analysis-instructions.txt')
# Scenario revenue rules: the reference keeps the prior level policy; Bear and Bull take the
# lowest and the highest-momentum baseline-family rules, not a tuned growth number.
RULES = {'bear': 'seasonal_naive', 'base': 'level_carry_forward', 'bull': 'drift'}
TEXT = {
    '2330.TW': {
        'bear': {'outcome': '非指引季度回到去年同季營收（年增0%；第四季仍用公司指引），營業利益率假設回到近四季最低的實際單季值。這是條件式情境，不是經濟下限或預測。',
                 'supporting': [('董事會核准約409.27億美元資本預算；TTM資本支出占營收33.3%，需求放緩時新產能折舊會壓縮利潤率。', ['tsmc-ev-board-capex', 'tsmc-capex-9m26', 'tsmc-capex-9m25', 'tsmc-capex-fy25'])],
                 'contradicting': [('10月營收年增59.7%；第四季營收指引中值按指引匯率高於去年同季的10,460.9億元。', ['tsmc-ev-oct-revenue', 'tsmc-q4-revenue-guidance-low', 'tsmc-q4-revenue-guidance-high', 'tsmc-q4-gm-guidance-low', 'tsmc-revenue-q4-25'])]},
        'base': {'outcome': '第四季用公司指引中值，其後各季維持第三季已報水準；營業利益率假設維持TTM。這是比較基準，不是最可能情境。',
                 'supporting': [('10月營收約5,868.9億元、年增59.7%（11/10公布），最新月營收未見反轉。', ['tsmc-ev-oct-revenue'])],
                 'contradicting': [('第三季營收年增50.9%，比第二季的年增率高14.9個百分點；若第三季水準偏高，延續會使後續季度偏高。', ['tsmc-revenue-q3-26', 'tsmc-revenue-q3-25', 'tsmc-revenue-q2-26', 'tsmc-revenue-q2-25'])]},
        'bull': {'outcome': '近四季平均單季增加額延續（第四季用公司指引），營業利益率假設維持近四季最高的實際單季值。這是線性動能外推，不是經濟上限或預測。',
                 'supporting': [('第三季先進製程占晶圓營收79%、營收年增50.9%。', ['tsmc-ev-q3-results'])],
                 'contradicting': [('第四季毛利率指引65–67%假設1美元兌31.8新台幣；新產能開出時程不在已查核證據中。', ['tsmc-q4-gm-guidance-low', 'tsmc-q4-gm-guidance-high'])]}},
    'DELL': {
        'bear': {'outcome': '非指引季度回到去年同季營收（年增0%；Q3仍用公司指引），營業利益率假設回到近四季最低的實際單季值。這是條件式情境，不是經濟下限或預測。',
                 'supporting': [('公司揭露記憶體供應限制與成本通膨，AI伺服器組合可能壓縮毛利率。', ['dell-memory-cost-risk'])],
                 'contradicting': [('期末AI伺服器積壓950億美元、單季AI訂單609億美元；積壓與訂單不等於已認列營收，轉換時程未揭露。', ['dell-ai-backlog-q2fy27', 'dell-ai-orders-q2fy27'])]},
        'base': {'outcome': 'Q3用公司指引，其後各季維持Q2已報水準；營業利益率假設維持TTM。這是比較基準，不是最可能情境。',
                 'supporting': [('FY2027 Q3營收指引490億美元，高於Q2已報的469.71億美元。', ['dell-guidance-q3fy27-revenue', 'dell-revenue-q2fy27'])],
                 'contradicting': [('2026-07-31存貨較2025-08-01增加195.2%、應收帳款增加52.6%，快於Q2營收年增57.7%；出貨若延後，Q2水準難以延續。', ['sec-DELL-inventory-2026-07-31-i', 'sec-DELL-inventory-2025-08-01-i', 'sec-DELL-accounts_receivable_net-2026-07-31-i', 'sec-DELL-accounts_receivable_net-2025-08-01-i', 'dell-revenue-q2fy27', 'dell-revenue-q2fy26'])]},
        'bull': {'outcome': '近四季平均單季增加額延續（Q3用公司指引），營業利益率假設維持近四季最高的實際單季值。這是線性動能外推，不是經濟上限或預測；積壓轉換時程未揭露，不換算成營收。',
                 'supporting': [('Q2期末AI伺服器積壓950億美元，Q2 AI伺服器營收164.01億美元。', ['dell-ai-backlog-q2fy27', 'dell-ai-server-revenue-q2fy27'])],
                 'contradicting': [('9/15完成四批合計50億美元優先票據發行；2026-07-31存貨較一年前增加195.2%；積壓可取消性與交付時程未揭露。', ['dell-debt-issuance', 'dell-notes-underwriting', 'sec-DELL-inventory-2026-07-31-i', 'sec-DELL-inventory-2025-08-01-i'])]}}}


def contract_hashes(run):
    # The analyst reads these four files before editing proposals; hashes record exactly what was read.
    return {name: hashlib.sha256((run / name).read_bytes()).hexdigest() for name in CONTRACT}


def do_prepare(reviewed_inputs=None):
    pending = {}
    if reviewed_inputs and len(reviewed_inputs) != len(SOURCES):
        raise SystemExit('Supply one reviewed input for each baseline company, in TSMC / DELL order')
    for i, source in enumerate(SOURCES):
        original = read_json(source / 'research_input.json')
        selected = read_json(reviewed_inputs[i]) if reviewed_inputs else original
        checked = ResearchInput.model_validate(selected)
        if checked.request != Request.model_validate(original['request']) or checked.dataset_kind != 'real':
            raise SystemExit('Reviewed input must preserve the baseline request, cutoff and real-data classification')
        root = source.parent.parent.parent / OUTPUT
        result = prepare(checked.request, root / 'runs', selected)
        run = Path(result['research_input_path']).parent
        write_json(run / 'contract-review.json', contract_hashes(run))
        write_json(run / 'data-review.json', {
            'baseline': str(source), 'reviewed_input': str(reviewed_inputs[i]) if reviewed_inputs else None,
            'baseline_evidence_count': len(original['evidence']), 'reviewed_evidence_count': len(checked.evidence),
            'request_unchanged': True, 'permissions_changed': False,
            'input_sha256': hashlib.sha256((run / 'research_input.json').read_bytes()).hexdigest()})
        pending[str(source)] = str(run)
        print(json.dumps({'source': str(source), 'run': str(run)}, ensure_ascii=False))
    write_json(Path('reports') / f'{OUTPUT}-pending.json', pending)


def quarter_margin(inputs, pick):
    rows = [q for q in inputs['analysis']['quarters'] if q['operating_margin'] is not None][-4:]
    q = pick(rows, key=lambda r: r['operating_margin'])
    ids = sorted(set(q['revenue']['evidence_ids']) | {i for p in inputs['series']['quarters']['operating_income']
                                                      if p['end'] == q['end'] for i in p['evidence_ids']})
    return q['operating_margin'], ids, q


def build_proposal(run, source):
    data = ResearchInput.model_validate(read_json(run / 'research_input.json'))
    inputs = read_json(run / 'investment_inputs.json')
    context = read_json(run / 'context.json')
    old = read_json(source / 'ai_proposal.json')
    entity = data.entities[0].entity_id
    candidates = inputs['forecast']['candidates']['candidates']
    horizon = inputs['forecast']['horizon']['label']
    ttm = inputs['forecast']['drivers']['operating_margin']
    proposal = {k: v for k, v in old.items() if k not in ('scenarios',)}
    proposal['input_sha256'] = context['input_sha256']
    proposal['prompt_version'] = context['prompt_version']
    proposal['model'] = 'Codex/Claude financial-model review of existing evidence (candidate paths)'
    if entity == 'DELL':
        # Keep the investor interpretation; the numbers and event dates are already in the tables.
        proposal['company_research'] = [dict(section, news=[{
            'text': 'AI訂單與積壓須經交付與收款驗證；票據發行與存貨、融資應收擴大同時發生。公司另揭露記憶體供應受限與成本上升，AI產品組合可能壓縮毛利率。',
            'evidence_ids': ['dell-q2-business', 'dell-notes-underwriting', 'dell-debt-issuance',
                             'dell-inventory-q2fy27', 'dell-memory-cost-risk', 'dell-gross-profit-q2fy27']}])
            for section in proposal['company_research']]
    scenarios = []
    for prior in old['scenarios']:
        name = prior['name']
        rule = RULES[name]
        c = candidates[rule]
        if c['status'] != 'applicable':
            raise SystemExit(f'{entity} {name}: candidate {rule} not applicable: {c["reason"]}')
        # The observed margin is a historical fact; applying it to the future NTM is a model assumption.
        if name == 'base':
            margin, margin_ids, quarter = ttm['value'], ttm['evidence_ids'], None
            margin_text = (f'觀察值：TTM 營業利益率 {ttm["value"]:.2f}%（{ttm["source_period"]}，歷史事實）。'
                           '用於 NTM 是模型假設，不是公司利潤率指引。')
        else:
            margin, margin_ids, quarter = quarter_margin(inputs, min if name == 'bear' else max)
            margin_text = (f'觀察值：{quarter["start"]}~{quarter["end"]} 單季營業利益率 {margin:.2f}%，為近四季'
                           f'{"最低" if name == "bear" else "最高"}（歷史事實）。用於 NTM 是模型假設，不代表未來的上下限。')
        text = TEXT[entity][name]
        s = {k: prior[k] for k in ('entity_id', 'name', 'valuation_metric', 'multiple_method', 'multiple', 'multiple_basis',
                                   'multiple_evidence_ids', 'catalysts', 'leading_indicators')}
        drivers = {
            'revenue_growth': {'name': 'revenue_growth', 'value': round(c['ntm_growth_percent'], 4), 'unit': '%',
                               'basis': 'model_assumption', 'evidence_ids': c['evidence_ids'][-8:],
                               'rationale': f'候選規則「{c["label"]}」：{c["assumes"]}',
                               'source_period': f'{c["training_window"][0]}~{c["training_window"][1]}',
                               'forecast_period': horizon, 'accounting_basis': 'reported',
                               'derivation': f'{c["formula"]}；指引季度改用公司指引，其餘季度依規則。程式逐季計算後四季相加，此值僅為預覽。'},
            'operating_margin': {'name': 'operating_margin', 'value': round(margin, 4), 'unit': '%', 'basis': 'model_assumption',
                                 'evidence_ids': margin_ids, 'rationale': margin_text,
                                 'source_period': ttm['source_period'] if quarter is None else f'{quarter["start"]}~{quarter["end"]}',
                                 'forecast_period': horizon, 'accounting_basis': 'reported', 'derivation': margin_text}}
        for key, d in prior['drivers'].items():
            if key not in drivers and key not in ('working_capital_intensity', 'ocf_residual_intensity'):
                drivers[key] = {**d, 'forecast_period': horizon}
        s.update(target_period=horizon, drivers=drivers, forecast_method='quarterly', growth_policy='level_carry_forward' if rule == 'level_carry_forward' else 'explicit_yoy',
                 revenue_candidate=rule, assumptions=[], financial_outcome=text['outcome'], valuation_metric_value=None,
                 guidance_fx_rate=old['scenarios'][1].get('guidance_fx_rate'),
                 guidance_fx_evidence_ids=old['scenarios'][1].get('guidance_fx_evidence_ids', []),
                 invalidation_conditions=['同 NTM 營收成長與營業利益率超出情境容忍帶；見結構化門檻。'],
                 supporting=[{'text': t, 'evidence_ids': ids} for t, ids in text['supporting']],
                 contradicting=[{'text': t, 'evidence_ids': ids} for t, ids in text['contradicting']])
        scenarios.append(s)
    proposal['scenarios'] = scenarios
    # Thresholds are set from the engine's own computed NTM outcome for each scenario.
    preview = engine.run(data, Proposal.model_validate({**proposal, 'scenarios': [
        {**s, 'operating_invalidations': []} for s in scenarios]}))
    computed = {r['name']: r for r in preview['scenarios']['scenarios']}
    # Every number in scenario prose must match cited evidence or an engine calculation, not just carry a citation.
    from stock_report.quality_gates import candidates_for, engine_values, numbers_in, supported
    index, values = {e.evidence_id: e for e in data.evidence}, engine_values(preview)
    bad = [f'{s["name"]}: {raw} in {p["text"][:30]}' for s in scenarios for p in s['supporting'] + s['contradicting']
           for raw, v in numbers_in(p['text']) if not supported(v, candidates_for(p['evidence_ids'], index, values))]
    missing = [i for s in scenarios for p in s['supporting'] + s['contradicting'] for i in p['evidence_ids'] if i not in index]
    if bad or missing:
        raise SystemExit(f'{entity}: unsupported scenario prose numbers {bad} or unknown evidence {missing}')
    for s in scenarios:
        r = computed[s['name']]
        rules = []
        for key, tolerance in (('revenue_growth', 5), ('operating_margin', 1)):
            value = r['drivers'][key]['value']
            for op in {'bear': ['gt'], 'bull': ['lt'], 'base': ['lt', 'gt']}[s['name']]:
                rules.append({'driver': key, 'operator': op, 'threshold': round(value + (tolerance if op == 'gt' else -tolerance), 4),
                              'period': horizon, 'rationale': '同期間實績超出情境容忍帶；門檻為模型設定，不是指引或信賴區間。'})
        s['operating_invalidations'] = rules
    for thesis in proposal['thesis']:
        thesis['invalidation'] = ['以同 NTM 期間營收成長及營業利益率檢驗情境；單季指引、訂單與積壓僅為領先觀察。']
    # Retained additional vintages change IDs, not the cited original filing facts. Remap only
    # exact same-source, same-period, same-publication, same-value records; never pick a revision.
    old_data = ResearchInput.model_validate(read_json(source / 'research_input.json'))
    new_ids = {e.evidence_id for e in data.evidence}
    def identity(e):
        return (e.license_source_id, e.source_record_id, e.financial_metric, e.period_start, e.period_end,
                e.period_basis, e.published_at, e.value, e.accounting_basis)
    by_fact = {identity(e): e.evidence_id for e in data.evidence}
    remap = {e.evidence_id: by_fact[identity(e)] for e in old_data.evidence
             if e.evidence_id not in new_ids and identity(e) in by_fact}
    def replace(value):
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        return remap.get(value, value) if isinstance(value, str) else value
    return replace(proposal)


def attribution(source, run):
    old, new = read_json(source / 'investment.json'), read_json(run / 'investment.json')
    old_s = {r['name']: r for r in old['scenarios']['scenarios']}
    new_s = {r['name']: r for r in new['scenarios']['scenarios']}
    rows = {}
    for name in ('bear', 'base', 'bull'):
        a, b = old_s[name], new_s[name]
        same = a['target_period'] == b['target_period']
        def pct(x, y):
            return (y / x - 1) * 100 if x and y is not None else None
        rows[name] = {'same_forecast_period': same, 'period_flag': None if same else f'{a["target_period"]} vs {b["target_period"]}',
                      'old_revenue_rule': a.get('drivers', {}).get('revenue_growth', {}).get('policy') or
                                          ('level_carry_forward' if any(q.get('growth_policy') == 'level_carry_forward' for q in a.get('quarterly_forecast', [])) else 'explicit_yoy_model_assumption'),
                      'new_revenue_rule': b.get('revenue_candidate'),
                      'old_ntm_revenue': a['statements']['revenue'], 'new_ntm_revenue': b['statements']['revenue'],
                      'revenue_change_percent': pct(a['statements']['revenue'], b['statements']['revenue']),
                      'old_operating_margin': a['statements']['operating_margin'], 'new_operating_margin': b['statements']['operating_margin'],
                      'old_eps': a['statements']['eps'], 'new_eps': b['statements']['eps'],
                      'eps_change_percent': pct(a['statements']['eps'], b['statements']['eps']),
                      'old_hypothetical_value': a['implied_price'], 'new_hypothetical_value': b['implied_price'],
                      'value_change_percent': pct(a['implied_price'], b['implied_price']),
                      'multiple_unchanged': a['valuation']['multiple'] == b['valuation']['multiple']}
        if same:
            bridge = change_report(new['forecast'], a, b)
            rows[name]['eps_sequential_bridge'] = bridge['eps_sequential_bridge']
            rows[name]['valuation_change_bridge'] = bridge['valuation_change_bridge']
            rows[name]['unexplained_eps_change'] = bridge['checks']['unexplained_eps_change']
        material = abs(rows[name]['eps_change_percent'] or 0) >= 10 or abs(rows[name]['value_change_percent'] or 0) >= 10
        rows[name]['material'] = material
        rows[name]['explanation'] = (
            '參考路徑營收規則與 TTM 利潤率不變，EPS 與每股價值不變；只改成候選路徑框架與投資人標示。' if name == 'base' and not material else
            f'營收由舊版自訂年增假設改為「{b.get("revenue_candidate")}」候選規則（指引季度仍用公司指引），營業利益率由模型自訂值改為「以近四季實際單季{"低" if name == "bear" else "高"}點作為 NTM 模型假設」；倍數不變，EPS 差異由營收路徑與利潤率假設解釋（見 eps_sequential_bridge）。')
    return {'source_run': str(source), 'run': str(run), 'engine_version_old': old.get('version'), 'engine_version_new': new.get('version'),
            'scenarios': rows,
            'data_acquisition': {'attempted': bool(read_json(run / 'data-review.json').get('reviewed_input')),
                                 'review': read_json(run / 'data-review.json'),
                                 'reason': ('New reviewed SEC filing vintages and actual statement components imported at the unchanged research cutoff; no raw responses retained'
                                            if read_json(run / 'data-review.json').get('reviewed_input') else
                                            'Offline regeneration of the existing reviewed evidence; no acquisition by this run'),
                                 'source_permissions_changed': False},
            'limitation': 'Old and new outputs are compared only for identical forecast periods; changes are attributed to explicit rule and margin inputs, not to forecast skill.'}


def do_finalize(qa):
    pending = read_json(Path('reports') / f'{OUTPUT}-pending.json')
    for source, run in pending.items():
        source, run = Path(source), Path(run)
        if contract_hashes(run) != read_json(run / 'contract-review.json'):
            raise SystemExit('Contract files changed after review; prepare a new run')
        proposal = build_proposal(run, source)
        write_json(run / 'ai_proposal.json', proposal)
        result = finalize(run, run / 'ai_proposal.json', Path('config/ratings.json'))
        inv = read_json(run / 'investment.json')
        for filename in ('report.pdf', 'research.json', 'manifest.json', 'forecast_qa.json'):
            assert (run / filename).stat().st_size > 0
        assert read_json(run / 'research.json')['report_grade'] == inv['gates']['grade']
        manifest = read_json(run / 'manifest.json')
        assert manifest['pdf_pages'] == len(PdfReader(run / 'report.pdf').pages)
        assert manifest['distribution'] == 'personal_use_only' and 'forecast_qa.json' in manifest['hashes']
        assert manifest['pdf_pages'] <= MAX_PAGES, f'{run}: {manifest["pdf_pages"]} pages exceeds the prior {MAX_PAGES}-page report'
        write_json(run / 'version_attribution.json', engine.jsonable(attribution(source, run)))
        if qa:
            subprocess.run([sys.executable, 'scripts/qa_pdf.py', '--report', str(run / 'report.pdf'),
                            '--output', str(run / 'visual-qa')], check=True)
        print(json.dumps({'run': str(run), 'grade': result.get('report_grade'), 'pages': manifest['pdf_pages'],
                          'critical_failures': inv['gates']['critical_failures'],
                          'scenarios': {r['name']: {'rule': r.get('revenue_candidate'), 'eps': r['statements']['eps'],
                                                    'value': r['implied_price_display']} for r in inv['scenarios']['scenarios']}},
                         ensure_ascii=False, default=str))


if __name__ == '__main__':
    print('SEC operator contact configured:', bool(os.environ.get('STOCK_REPORT_SEC_CONTACT')))
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--finalize', action='store_true')
    parser.add_argument('--qa', action='store_true')
    parser.add_argument('--reviewed-input', type=Path, action='append', help='Reviewed import input for TSMC then DELL; requests/cutoffs must match baselines')
    args = parser.parse_args()
    if args.prepare:
        do_prepare(args.reviewed_input)
    if args.finalize:
        do_finalize(args.qa)
    if not (args.prepare or args.finalize):
        for source in SOURCES:
            inv = engine.run(ResearchInput.model_validate(read_json(source / 'research_input.json')))
            print(source, json.dumps({n: (c['status'], c['ntm_growth_percent']) for n, c in inv['forecast']['candidates']['candidates'].items()}))
