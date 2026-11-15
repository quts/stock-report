"""One-container offline acceptance run. Does not fetch data, call a model or enable sources."""
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from stock_report.diagnostics import doctor
from stock_report.storage import write_json

output = Path('reports/phase-0')
output.mkdir(parents=True, exist_ok=True)
steps = [
    ('tests', [sys.executable, '-m', 'pytest', '-q', '--junitxml=reports/phase-0/provider-increment-junit.xml']),
    ('demo', [sys.executable, 'scripts/demo.py']),
    ('qa', [sys.executable, 'scripts/qa_pdf.py']),
    ('bundle', [sys.executable, 'scripts/review_bundle.py']),
]
results = []
for name, command in steps:
    result = subprocess.run(command, check=False)
    results.append({'step': name, 'exit_code': result.returncode})
    if result.returncode:
        write_json(output / 'acceptance.json', {'status': 'failed', 'steps': results})
        raise SystemExit(result.returncode)
readiness = doctor()
report = {'status': 'offline_acceptance_passed', 'completed_at': datetime.now(timezone.utc).isoformat(),
          'steps': results, 'readiness': readiness, 'network_called': False,
          'model_evaluation': 'Recorded session batch; not reexecuted by verify' if (output/'llm-evaluation-current.json').exists()
                              else 'NOT_RUN; synthetic analysis verifies software only',
          'formal_mvp_go': False, 'real_research_e2e': 'NOT_PASSED; ' + ('no source has a sufficient recorded review' if not readiness['enabled_sources']
                                                 else 'enabled sources alone are not an end-to-end real research run')}
write_json(output / 'acceptance.json', report)
print((output / 'acceptance.json').resolve())
