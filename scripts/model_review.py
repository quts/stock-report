"""Reproduce reviewed reports using existing evidence, without new network data or permissions."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from stock_report import engine
from stock_report.models import Request, ResearchInput
from stock_report.pipeline import prepare, finalize
from stock_report.storage import read_json, write_json
from pypdf import PdfReader

SOURCES = [Path('reports/tsmc-2330-20261115/v4/runs/d6c4d8f114d34f8c93c2c65a5ddafe87'),
           Path('reports/dell-v2-20261115/v4/runs/8c7f132e16d7499ab58a03451480072c')]


def regenerate(source):
    original = read_json(source/'research_input.json')
    old = read_json(source/'investment.json')
    root = source.parent.parent.parent/'model-corrected-20261115'
    result = prepare(Request.model_validate(original['request']), root/'runs', original)
    run = Path(result['research_input_path']).parent
    contract = {name: (run/name).read_text() for name in
                ('research_input.json', 'context.json', 'proposal.schema.json', 'analysis-instructions.txt')}
    write_json(run/'contract-review.json', {n: hashlib.sha256(v.encode()).hexdigest() for n,v in contract.items()})
    proposal = read_json(source/'ai_proposal.json')
    proposal['input_sha256'] = read_json(run/'prepared.json')['input_sha256']
    proposal['model'] = 'Codex financial-model review of existing evidence'
    data = ResearchInput.model_validate(json.loads(contract['research_input.json']))
    a = engine.run(data)['forecast']
    is_tw = data.entities[0].entity_id == '2330.TW'
    rev = a['base']['revenue_ttm']['value']
    growth = ((53.3e9 * 31.8 * 4 if is_tw else 49e9 * 4) / rev - 1) * 100
    for scenario in proposal['scenarios']:
        name = scenario['name']
        scenario['leading_indicators'] = scenario['catalysts']
        if name == 'base':
            d = scenario['drivers']['revenue_growth']
            d.update(value=growth, basis='guidance_informed_model',
                     source_period='Q4 2026' if is_tw else 'FY2027 Q3 (ends 2026-10-30)',
                     forecast_period=scenario['target_period'], accounting_basis='reported',
                     derivation=(f'(53.3bn USD × 31.8 TWD/USD × 4 / TTM {rev:.0f}) − 1 = {growth:.4f}%' if is_tw else
                                 f'(49bn USD × 4 / TTM {rev:.0f}) − 1 = {growth:.4f}%') +
                                '；其後三季持平、無季節性；未驗證持續性，非全年指引。')
            d['rationale'] = '單季指引固定 run-rate 敏感度；公司未提供此 NTM 成長率，其後三季為模型假設。'
            if is_tw:
                d['evidence_ids'].append('tsmc-q4-gm-guidance-low')  # Contains the company's FX assumption.
            scenario['financial_outcome'] = '單季指引固定年化的機械敏感度，不代表最可能路徑；利潤率與稅率延用歷史，FCF 受殘差與回測樣本不足限制。'
        for key,d in scenario['drivers'].items():
            d.setdefault('source_period', f'{a["base"]["revenue_ttm"]["start"]}~{a["base"]["revenue_ttm"]["end"]}')
            d.setdefault('forecast_period', scenario['target_period'])
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
            value = scenario['drivers'][key]['value']
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
    summary = {'source_run': str(source), 'run': str(run), 'result': final,
               'old_base': next(s for s in old['scenarios']['scenarios'] if s['name'] == 'base'),
               'new_base': next(s for s in inv['scenarios']['scenarios'] if s['name'] == 'base'),
               'reconciliation': inv['forecast']['reconciliation'], 'backtest': inv['forecast']['backtest'], 'gates': inv['gates']}
    write_json(root/'comparison.json', summary)
    print(json.dumps({'run': str(run), 'base_growth': growth, 'base_outputs': summary['new_base']['statements'],
                      'residual': summary['reconciliation']['residual'], 'backtest': summary['backtest'],
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
            print(json.dumps(engine.public(inv['forecast']['backtest']), ensure_ascii=False))
