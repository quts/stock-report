"""Reproduce fixed-cutoff reports without changing evidence or scenario assumptions."""
import hashlib
import json
from pathlib import Path
from pypdf import PdfReader
from stock_report import engine
from stock_report.models import Proposal, ResearchInput
from stock_report.pipeline import prepare, finalize
from stock_report.storage import read_json, write_json
from stock_report.backtesting import scores, verified_metric_rows

BASELINES = {
    'TSMC': Path('reports/tsmc-2330-20261115/pit-catalog-reviewed-20261116/runs/19d8a1e39aaf472593c4714825200559'),
    'DELL': Path('reports/dell-v2-20261115/pit-catalog-reviewed-20261116/runs/ee74d810f1474c988c7572f263dc5f96'),
}
CONTRACT = ('research_input.json', 'context.json', 'proposal.schema.json', 'analysis-instructions.txt')


def measurements(inv):
    pit = inv['retrospective']['point_in_time']
    return {'filing_coverage': {k: inv['retrospective']['filing_dataset'][k] for k in
                              ('verified_original_count', 'verified_availability_count', 'coverage')},
            'candidate_funnel': pit['funnel'], 'root_causes': pit['root_cause_counts'],
            'candidate_scores': pit['verified_scores_by_method'],
            'full_forecast_funnel': inv['forecast']['backtest']['funnel'],
            'full_forecast_metrics': scores(verified_metric_rows(inv['forecast']['backtest']['rows'])),
            'full_forecast_baselines': inv['forecast']['backtest']['verified_baseline_comparison'],
            'market_data': inv['market_data'], 'valuation_references': inv['valuation']['references'],
            'reverse_valuation': inv['reverse_valuation'], 'horizon': inv['forecast']['horizon'],
            'selected_candidate': inv['forecast']['candidates']['selected']}


def summary(comparison):
    lines = ['# PIT evaluation before / after', '',
             'Same reviewed evidence, research cutoff, scenario EPS, multiples and values. Private use only.', '',
             'Old WAPE is recomputed from saved old row-level errors, without rerunning old forecasts.',
             'F4Q means four fiscal quarters after the last report, not twelve months after research as-of.',
             'Changed sample counts change the population; error changes alone are not improved model skill.', '']
    for name, row in comparison['companies'].items():
        a, b = row['before'], row['after']
        lines += [f'## {name}', '', f'As of {row["as_of"]}; {row["pages"]} PDF pages.', '',
                  '| Measure | Before | After |', '| --- | ---: | ---: |']
        for label, old, new in [
            ('Revenue periods', a['candidate_funnel']['financial_periods'], b['candidate_funnel']['financial_periods']),
            ('Catalog-verified eligible revenue origins', a['candidate_funnel']['catalog_verified_origins'], b['candidate_funnel']['catalog_verified_origins']),
            ('Verified full-model revenue scored samples', a['full_forecast_metrics']['revenue']['sample_size'], b['full_forecast_metrics']['revenue']['sample_size']),
            ('Revenue MAE (issuer reporting currency)', a['full_forecast_metrics']['revenue']['mae'], b['full_forecast_metrics']['revenue']['mae']),
            ('Revenue WAPE %', a['full_forecast_metrics']['revenue']['wape_percent'], b['full_forecast_metrics']['revenue']['wape_percent']),
            ('Revenue signed bias (issuer reporting currency)', a['full_forecast_metrics']['revenue']['bias'], b['full_forecast_metrics']['revenue']['bias']),
            ('Verified EPS scored samples', a['full_forecast_metrics']['eps']['sample_size'], b['full_forecast_metrics']['eps']['sample_size']),
            ('Price observations', a['market_data']['history_points'], b['market_data']['history_points']),
            ('Historical P/E observations', len(a['valuation_references']['historical_pe']['points']), len(b['valuation_references']['historical_pe']['points'])),
            ('Reverse valuation available', a['reverse_valuation']['status'], b['reverse_valuation']['status']),
            ('Preferred candidate', a['selected_candidate'], b['selected_candidate'])]:
            lines.append(f'| {label} | {old} | {new} |')
        lines += ['', 'Verified raw revenue rules (paired comparisons use identical origins and actuals):', '',
                  '| Rule | F4Q n | MAE | WAPE % | Bias | MAE skill vs seasonal naive | MAE skill vs latest level |',
                  '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
        raw = b['candidate_scores']['raw_rule']
        for name_, values in raw['per_candidate'].items():
            s = values['ntm']
            skills = [raw['pairwise'].get(name_, {}).get(base, {}).get('ntm', {}).get('mae_skill')
                      for base in ('seasonal_naive', 'level_carry_forward')]
            lines.append(f'| {name_} | {s["samples"]} | {s["mae"]} | {s["wape_percent"]} | {s["bias"]} | {skills[0]} | {skills[1]} |')
        lines += ['', 'MAE skill = 1 - candidate MAE / baseline MAE; negative is worse. Undefined if baseline MAE is zero.',
                  'Raw rules do not establish skill for deployed guidance overlays. Overlapping windows are not independent.',
                  f'Root causes: `{json.dumps(b["root_causes"], ensure_ascii=False)}`', '',
                  f'Completed unreported quarters: `{b["horizon"]["completed_unreported_quarters"]}`', '',
                  f'Outputs: `{row["run"]}` (container /work maps to repository root).', '']
    return '\n'.join(lines)


def replace_periods(value, old, new):
    if isinstance(value, dict):
        return {k: replace_periods(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_periods(v, old, new) for v in value]
    if isinstance(value, str):
        return value.replace(old, new).replace('NTM', 'F4Q')
    return value


def main():
    comparison = {'purpose': 'Fixed-cutoff verified PIT evaluation; unchanged evidence and scenario inputs', 'companies': {}}
    for name, source in BASELINES.items():
        original = read_json(source / 'research_input.json')
        data = ResearchInput.model_validate(original)
        before = read_json(source / 'investment.json')
        root = source.parent.parent.parent / 'pit-evaluation-20261117'
        result = prepare(data.request, root / 'runs', original)
        run = Path(result['research_input_path']).parent
        # Read every required analyst contract before touching the proposal; retain only hashes.
        contract = {f: (run / f).read_text() for f in CONTRACT}
        write_json(run / 'contract-review.json', {f: hashlib.sha256(t.encode()).hexdigest() for f, t in contract.items()})
        inputs = read_json(run / 'investment_inputs.json')
        proposal = replace_periods(read_json(source / 'ai_proposal.json'),
                                  before['forecast']['horizon']['label'], inputs['forecast']['horizon']['label'])
        context = json.loads(contract['context.json'])
        proposal.update(input_sha256=context['input_sha256'], prompt_version=context['prompt_version'],
                        model='Codex focused PIT evaluation review; unchanged scenario assumptions')
        Proposal.model_validate(proposal)
        write_json(run / 'ai_proposal.json', proposal)
        finalize(run, run / 'ai_proposal.json', Path('config/ratings.json'))
        after = read_json(run / 'investment.json')
        manifest = read_json(run / 'manifest.json')
        research = read_json(run / 'research.json')
        assert manifest['pdf_pages'] == len(PdfReader(run / 'report.pdf').pages)
        assert manifest['pdf_pages'] <= len(PdfReader(source / 'report.pdf').pages)
        assert manifest['distribution'] == 'personal_use_only'
        assert research['report_grade'] == after['gates']['grade']
        assert read_json(run / 'research_input.json') == original
        for a, b in zip(before['scenarios']['scenarios'], after['scenarios']['scenarios']):
            assert a['statements']['eps'] == b['statements']['eps']
            assert a['valuation']['multiple'] == b['valuation']['multiple']
            assert a['implied_price'] == b['implied_price']
        row = {'source_run': str(source), 'run': str(run), 'pages': manifest['pdf_pages'],
               'as_of': data.request.research_as_of.isoformat(), 'before': measurements(before), 'after': measurements(after),
               'evidence_unchanged': True, 'scenario_eps_multiples_values_unchanged': True}
        write_json(run / 'evaluation-comparison.json', row)
        comparison['companies'][name] = row
        print(json.dumps({'company': name, 'run': str(run), 'funnel': row['after']['full_forecast_funnel'],
                          'metrics': row['after']['full_forecast_metrics']}, default=str))
    write_json(Path('reports/pit-evaluation-comparison-20261117.json'), comparison)
    Path('reports/pit-evaluation-comparison-20261117.md').write_text(summary(comparison))


if __name__ == '__main__':
    main()
