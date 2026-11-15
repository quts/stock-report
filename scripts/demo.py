"""Generate explicitly synthetic examples and a replay audit."""
from pathlib import Path
from stock_report.models import Request
from stock_report.pipeline import prepare, finalize, replay, load_prepared
from stock_report.analysis import synthetic_proposal
from stock_report.events import load_universe, rank_dossier, synthetic_dossier
from stock_report.storage import read_json, write_json

root = Path('reports/demo')
results = []
for name in ('stock-nvda', 'stock-twse', 'stock-tpex', 'event'):
    request = Request.model_validate(read_json(Path('examples') / f'{name}.json'))
    ranking_path = None
    if name == 'event':
        bundle, ranking = rank_dossier(synthetic_dossier(request), load_universe())
        if bundle is None:
            raise ValueError('Synthetic event ranking did not produce a bundle')
        ranking_path = root / 'event-ranking.json'
        write_json(ranking_path, ranking)
        result = prepare(request, root, bundle.model_dump(mode='json'), synthetic_source=True)
    else:
        result = prepare(request, root)
    run = Path(result['research_input_path']).parent
    _, data, _ = load_prepared(run)
    write_json(run / 'ai_proposal.json', synthetic_proposal(data))
    result = finalize(run, run / 'ai_proposal.json', Path('config/ratings.json'))
    result['example'] = name
    if ranking_path:
        result['ranking_path'] = str(ranking_path.resolve())
    results.append(result)
results.append(replay(Path(results[0]['report_path']).parent, root))
write_json(root / 'index.json', results)
print((root / 'index.json').resolve())
