"""Create a large-type companion PDF from an already finalized, private report run."""
import argparse
import hashlib
from pathlib import Path
import re
import unicodedata
from pypdf import PdfReader
from stock_report.models import Decision, Proposal, ResearchInput
from stock_report.render import render
from stock_report.storage import read_json, write_json


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def numbers(reader):
    text = unicodedata.normalize('NFKC', '\n'.join(p.extract_text(extraction_mode='layout') or '' for p in reader.pages))
    return set(re.findall(r'(?<![a-zA-Z])[-+]?\d[\d,]*(?:\.\d+)?%?', text))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    run = args.run
    manifest = read_json(run / 'manifest.json')
    assert manifest['distribution'] == 'personal_use_only'
    source_files = ('research_input.json', 'ai_proposal.json', 'decision.json', 'rating_mapping.json', 'investment.json', 'report.pdf')
    hashes = {f: file_hash(run / f) for f in source_files}
    assert all(manifest['hashes'][f] == h for f, h in hashes.items())
    data = ResearchInput.model_validate(read_json(run / 'research_input.json'))
    proposal = Proposal.model_validate(read_json(run / 'ai_proposal.json'))
    decision = Decision.model_validate(read_json(run / 'decision.json'))
    output = run / 'report-mobile.pdf'
    # Render saved approved analytical outputs; no research, assumptions or source clocks change.
    pages = render(data, decision, read_json(run / 'rating_mapping.json'), output,
                   company_research=proposal.company_research, investment=read_json(run / 'investment.json'), layout='mobile')
    reader = PdfReader(output)
    available = numbers(reader)
    missing = numbers(PdfReader(run / 'report.pdf')) - available
    # The old narrow candidate table concatenates the last quarterly value and total
    # even in layout extraction (e.g. 14,00058,000). Accept only exact token partitions.
    def partitioned(token):
        return any(token[:i] in available and token[i:] in available for i in range(1, len(token)))
    missing = {token for token in missing if not partitioned(token)}
    assert not missing, f'Numeric content lost in reflow: {sorted(missing)}'
    assert len(reader.pages) == pages
    assert all(file_hash(run / f) == h for f, h in hashes.items())
    write_json(run / 'mobile-manifest.json', {'distribution': 'personal_use_only', 'source_run': str(run),
               'research_as_of': data.request.research_as_of.isoformat(), 'source_hashes': hashes,
               'report': output.name, 'sha256': file_hash(output), 'pdf_pages': pages,
               'layout': {'page_mm': [135, 240], 'body_pt': 14, 'record_pt': 13, 'note_pt': 12.5,
                          'tables': 'single_column_label_value_records'},
               'checks': {'original_files_unchanged': True, 'numeric_tokens_preserved': True}})
    print(output, pages, 'pages; source files and numeric content preserved')


if __name__ == '__main__':
    main()
