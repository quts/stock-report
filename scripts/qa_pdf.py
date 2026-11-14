"""Rasterize both report types for manual visual verification in the container."""
from pathlib import Path
import subprocess
from stock_report.storage import read_json

index = read_json('reports/demo/index.json')
out = Path('reports/qa')
out.mkdir(parents=True,exist_ok=True)
for name in ('stock-nvda','event'):
    result = next(r for r in index if r.get('example') == name)
    subprocess.run(['pdftoppm','-scale-to','1200','-png',result['report_path'],str(out/name)],check=True)
print(out.resolve())
