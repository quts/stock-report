"""Rasterize reports for visual verification; preserve the default demo QA workflow."""
import argparse
from pathlib import Path
import subprocess
from stock_report.storage import read_json

parser = argparse.ArgumentParser()
parser.add_argument('--report', type=Path, help='Inspect one actual report instead of demo reports')
parser.add_argument('--output', type=Path, default=Path('reports/qa'))
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
if args.report:
    reports = [('report', args.report)]
else:
    index = read_json('reports/demo/index.json')
    reports = [(name, Path(next(r for r in index if r.get('example') == name)['report_path']))
               for name in ('stock-nvda', 'event')]
for name, report in reports:
    subprocess.run(['pdftoppm', '-scale-to', '1200', '-png', str(report), str(args.output/name)], check=True)
    subprocess.run(['pdftotext', '-layout', str(report), str(args.output/(name+'.txt'))], check=True)
print(args.output.resolve())
