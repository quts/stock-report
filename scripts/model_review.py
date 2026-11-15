"""Reproduce reviewed reports using existing evidence, without new network data or permissions."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from stock_report import engine
from stock_report import forecast
from stock_report.models import Request, ResearchInput
from stock_report.pipeline import prepare, finalize
from stock_report.storage import read_json, write_json
from pypdf import PdfReader

SOURCES = [Path('reports/tsmc-2330-20261115/model-corrected-20261115/runs/d841541f67fd4f0599e13a75342b0b81'),
           Path('reports/dell-v2-20261115/model-corrected-20261115/runs/0f7947519c334c3cbab177e4155d93a2')]


def regenerate(source):
    original = read_json(source/'research_input.json')
    old = read_json(source/'investment.json')
    root = source.parent.parent.parent/'forecast-reliability-20261116'
    result = prepare(Request.model_validate(original['request']), root/'runs', original)
    run = Path(result['research_input_path']).parent
    contract = {name: (run/name).read_text() for name in
                ('research_input.json', 'context.json', 'proposal.schema.json', 'analysis-instructions.txt')}
    write_json(run/'contract-review.json', {n: hashlib.sha256(v.encode()).hexdigest() for n,v in contract.items()})
    proposal = read_json(source/'ai_proposal.json')
    proposal['input_sha256'] = read_json(run/'prepared.json')['input_sha256']
    proposal['prompt_version'] = json.loads(contract['context.json'])['prompt_version']
    proposal['model'] = 'Codex financial-model review of existing evidence'
    data = ResearchInput.model_validate(json.loads(contract['research_input.json']))
    reviewed = engine.run(data)
    a = reviewed['forecast']
    is_tw = data.entities[0].entity_id == '2330.TW'
    rev = a['base']['revenue_ttm']['value']
    seed = forecast.trend_drivers(a)
    growth, margin = seed['revenue_growth']['value'], seed['operating_margin']['value']
    # Preview the same model that finalize uses; this is not a separate source of financial facts.
    preview, _ = forecast.quarterly_project(a, seed, forecast.guidance_records(data, data.entities[0].entity_id),
                                           31.8 if is_tw else None)
    base_growth, base_margin = preview['revenue_growth'], preview['operating_margin']
    for scenario in proposal['scenarios']:
        name = scenario['name']
        scenario['target_period'] = a['horizon']['label']
        scenario['forecast_method'] = 'quarterly'
        scenario['leading_indicators'] = scenario['catalysts']
        if name == 'base':
            d = scenario['drivers']['revenue_growth']
            d.update(value=growth, basis='model_assumption', evidence_ids=a['drivers']['revenue_growth']['evidence_ids'],
                     source_period='最近兩季營收年增率',
                     forecast_period=scenario['target_period'], accounting_basis='reported',
                     derivation=f'各季去年同期營收 × (1 + 最近兩季年增率中位數 {growth:.4f}%)；僅指引實際季度用其同口徑中值覆蓋，四季相加。')
            d['rationale'] = '歷史同季季節性與最近兩季成長趨勢的機械延用，不是已驗證預測；不將指引外推其他季度。'
            if is_tw:
                scenario['guidance_fx_rate'] = 31.8
                scenario['guidance_fx_evidence_ids'] = ['tsmc-q4-gm-guidance-low']
            scenario['drivers']['operating_margin']['value'] = margin
            scenario['drivers']['operating_margin']['basis'] = 'model_assumption'
            scenario['drivers']['operating_margin']['derivation'] = '最近兩季營業利益率加權平均，疊加去年同季對 TTM 的季節差；非公司年度指引。'
            scenario['financial_outcome'] = '四季逐季推估：去年同季季節性×最近兩季成長趨勢；公司指引僅覆蓋其季度。營益率採近期趨勢與歷史季節差；OCF／FCF 因缺完整現金流量表勾稽而停止預測。單季年化僅另存敏感度，不作 Base。'
        else:
            # Keep the original stress cases unless they no longer bracket the new Base.
            if name == 'bull':
                scenario['drivers']['revenue_growth']['value'] = max(scenario['drivers']['revenue_growth']['value'], base_growth + 10)
                scenario['drivers']['operating_margin']['value'] = max(scenario['drivers']['operating_margin']['value'], base_margin + 1)
        for key,d in scenario['drivers'].items():
            d.setdefault('source_period', f'{a["base"]["revenue_ttm"]["start"]}~{a["base"]["revenue_ttm"]["end"]}')
            d['forecast_period'] = scenario['target_period']
            d.setdefault('accounting_basis', 'reported')
            d.setdefault('derivation', '獨立模型情境設定，非證據直接推得；引用僅提供背景。' + d['rationale'])
        if 'operating_margin' not in scenario['drivers']:
            scenario['drivers']['operating_margin'] = {
                **{k:v for k,v in a['drivers']['operating_margin'].items() if k not in ('status',)},
                'name': 'operating_margin', 'unit': '%', 'basis': 'model_assumption',
                'forecast_period': scenario['target_period'],
                'derivation': 'TTM 營業利益率持平為模型假設，不是公司利潤率指引。'}
        rules = []
        for key,tolerance in [('revenue_growth', 5), ('operating_margin', 1)]:
            value = (base_growth if key == 'revenue_growth' else base_margin) if name == 'base' else scenario['drivers'][key]['value']
            operators = ['gt'] if name == 'bear' else ['lt'] if name == 'bull' else ['lt', 'gt']
            for op in operators:
                rules.append({'driver': key, 'operator': op, 'threshold': round(value + (tolerance if op == 'gt' else -tolerance), 4),
                              'period': scenario['target_period'],
                              'rationale': '同期間實績超出情境容忍帶；門檻為模型設定，不是指引或信賴區間。'})
        scenario['operating_invalidations'] = rules
        scenario['invalidation_conditions'] = ['同 NTM 營收成長與營業利益率超出情境容忍帶；見結構化門檻。']
        if not is_tw:
            scenario['financial_outcome'] += ' 訂單與積壓不等於認列營收或收款；未假設全數轉換。'
    for section in proposal['company_research']:
        for field in ('overview', 'key_takeaways', 'fundamentals', 'news', 'earnings_preview', 'monitoring'):
            for p in section[field]:
                p['text'] = p['text'].replace('現金轉換尚可：', '歷史現金轉換：').replace('獲利能轉成現金：', '歷史現金轉換：')
                if '現金轉換循環' in p['text']:
                    p['text'] += ' 天數使用期末餘額／TTM流量，非平均餘額；負循環不保證收款。'
    for thesis in proposal['thesis']:
        thesis['invalidation'] = ['以同期間營收成長及營業利益率檢驗情境；單季指引、訂單與庫存僅為領先觀察。']
    for outlook in proposal['outlooks']:
        outlook['invalidation_conditions'] = ['方向為 insufficient；持續核對同期間營運假設，單季偏差不是全年情境失效。']
    write_json(run/'ai_proposal.json', proposal)
    final = finalize(run, run/'ai_proposal.json', Path('config/ratings.json'))
    inv = read_json(run/'investment.json')
    for filename in ('report.pdf', 'research.json', 'manifest.json'):
        assert (run/filename).stat().st_size > 0
    assert read_json(run/'research.json')['report_grade'] == inv['gates']['grade']
    manifest = read_json(run/'manifest.json')
    assert manifest['pdf_pages'] == len(PdfReader(run/'report.pdf').pages)
    assert manifest['distribution'] == 'personal_use_only'
    summary = {'source_run': str(source), 'run': str(run), 'result': final,
               'old_base': next(s for s in old['scenarios']['scenarios'] if s['name'] == 'base'),
               'new_base': next(s for s in inv['scenarios']['scenarios'] if s['name'] == 'base'),
               'old_reconciliation': old['forecast']['reconciliation'], 'old_backtest': old['forecast']['backtest'],
               'reconciliation': inv['forecast']['reconciliation'], 'backtest': inv['forecast']['backtest'], 'gates': inv['gates'],
               'mechanical_annualization_sensitivity': {
                   'method': 'single_quarter_times_four_not_base',
                   'revenue': 53.3e9 * 31.8 * 4 if is_tw else 49e9 * 4,
                   'limitation': '固定三季持平、無季節性的純敏感度；不納入 Base 或回測。'}}
    write_json(root/'comparison.json', summary)
    print(json.dumps({'run': str(run), 'base_growth': growth, 'base_outputs': summary['new_base']['statements'],
                      'residual': summary['reconciliation']['residual'], 'backtest_metrics': summary['backtest']['metrics'],
                      'revised_metrics': summary['backtest']['revised_diagnostic']['metrics'],
                      'critical_failures': inv['gates']['critical_failures']}, ensure_ascii=False))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--regenerate', action='store_true')
    parser.add_argument('--qa', action='store_true')
    args = parser.parse_args()
    for source in SOURCES:
        if args.regenerate:
            summary = regenerate(source)
            if args.qa:
                run = Path(summary['run'])
                subprocess.run([sys.executable, 'scripts/qa_pdf.py', '--report', str(run/'report.pdf'),
                                '--output', str(run/'visual-qa')], check=True)
                print('PDF pages:', len(PdfReader(run/'report.pdf').pages))
        else:
            inv = engine.run(ResearchInput.model_validate(read_json(source/'research_input.json')))
            print(source)
            print('Metrics:', sorted(inv['series']['quarters']))
            print('Balances:', {k: [str(p['end']) for p in v] for k,v in inv['series']['instants'].items() if k in ('inventory', 'accounts_payable')})
            print(json.dumps(engine.public(inv['forecast']['reconciliation']), ensure_ascii=False))
            print('PIT metrics:', json.dumps(inv['forecast']['backtest']['metrics']))
            print('Revised metrics:', json.dumps(inv['forecast']['backtest']['revised_diagnostic']['metrics']))
