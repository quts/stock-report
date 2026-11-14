"""Generate explicitly synthetic examples and a replay audit."""
from pathlib import Path
from stock_report.models import Request
from stock_report.pipeline import prepare, finalize, replay, load_prepared
from stock_report.analysis import synthetic_proposal
from stock_report.storage import read_json, write_json

root = Path('reports/demo')
results = []
for name in ('stock-nvda', 'stock-twse', 'stock-tpex', 'event'):
    request = Request.model_validate(read_json(Path('examples') / f'{name}.json'))
    result = prepare(request, root)
    run = Path(result['research_input_path']).parent
    _, data, _ = load_prepared(run)
    write_json(run / 'ai_proposal.json', synthetic_proposal(data))
    result = finalize(run, run / 'ai_proposal.json', Path('config/ratings.json'))
    result['example'] = name
    results.append(result)
results.append(replay(Path(results[0]['report_path']).parent, root))
write_json(root / 'index.json', results)
print((root / 'index.json').resolve())
