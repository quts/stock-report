"""Auditable filing catalog from already reviewed official SEC sources.

The catalog verifies filing identity, not values: for every accession cited by the reviewed
evidence it records the issuer CIK and conformed name, form, original/amendment status,
reporting period, filing date and the source-declared acceptance timestamp.

Scope is respected exactly:

* `sec-submissions-history` is reviewed for `https://data.sec.gov/submissions/CIK{cik}.json` only.
  Its `filings.recent` block is parsed; the child history files it lists are *not* fetched,
  because they are separate documents outside the reviewed base-file scope.
* `sec-edgar-archives-filing` is reviewed for archive documents under one issuer accession path.
  For a cited accession absent from `filings.recent`, only that accession's
  `{accession}-index-headers.html` header document is read.

Only normalized metadata is kept. Raw bodies and response hashes are never persisted (both
sources keep `store_raw_response` and `store_response_hash` closed). The operator SEC contact
is supplied by the caller from the environment and never echoed or stored.

Acceptance timestamps are recorded exactly as the source declares them. Their timezone
semantics are not independently verified, so they never move an evidence clock or a cutoff
earlier; evidence keeps its own (conservative) preserved clock.
"""
from datetime import date, datetime, time, timedelta, timezone
import html
import re
from .storage import read_json

SUBMISSIONS = 'sec-submissions-history'
ARCHIVES = 'sec-edgar-archives-filing'
SUBMISSIONS_URL = 'https://data.sec.gov/submissions/CIK{cik}.json'
HEADER_URL = 'https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{accession}-index-headers.html'
PERIODIC = ('10-Q', '10-K', '10-QT', '10-KT', '20-F', '40-F')
FORMS = PERIODIC + tuple(f + '/A' for f in PERIODIC) + ('6-K', '6-K/A', '8-K', '8-K/A')
ACCESSION = re.compile(r'^\d{10}-\d{2}-\d{6}$')
MAX_HEADER_LOOKUPS = 200
# Date-only filing stamps are bounded by the end of the US Eastern filing day (fixed UTC-05,
# the same conservative convention the collectors use for `filed` dates).
EASTERN_FIXED = timezone(timedelta(hours=-5))


class CatalogError(ValueError):
    """A fixed diagnostic code; messages never contain response content."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def issuer_row(ticker):
    rows = [r for r in read_json('config/sec-entities.json')['entities'] if r['ticker'] == ticker]
    if len(rows) != 1:
        raise ValueError('Issuer must be uniquely configured in config/sec-entities.json')
    return rows[0]


def _name_matches(row, name):
    expected = row.get('edgar_conformed_name_contains') or row['expected_entity_name_contains']
    return expected.casefold() in str(name or '').casefold()


def filing_day_upper_bound(day):
    # Match the collector's declared date-bound convention and second resolution.
    return datetime.combine(day, time(23, 59, 59), EASTERN_FIXED)


def parse_submissions(payload, row, as_of):
    """Catalog entries from `filings.recent` only; child history files are listed, never fetched."""
    if int(payload.get('cik', -1)) != int(row['cik']):
        raise ValueError('Submissions response belongs to another CIK')
    if not _name_matches(row, payload.get('name')):
        raise ValueError('Submissions response issuer name does not match the configured issuer')
    recent = (payload.get('filings') or {}).get('recent') or {}
    fields = ('accessionNumber', 'form', 'filingDate', 'reportDate')
    if not all(isinstance(recent.get(k), list) for k in fields):
        raise ValueError('SEC submissions contract changed')
    size = len(recent['form'])
    if size > 20000 or any(len(recent[k]) != size for k in fields):
        raise ValueError('SEC filing columns have inconsistent lengths')
    accepted = recent.get('acceptanceDateTime') if isinstance(recent.get('acceptanceDateTime'), list) else []
    entries, after_cutoff = [], 0
    for i, form in enumerate(recent['form']):
        if form not in FORMS:
            continue
        accession = recent['accessionNumber'][i]
        if not ACCESSION.fullmatch(str(accession)):
            raise ValueError('Unexpected SEC accession format')
        filed = date.fromisoformat(recent['filingDate'][i])
        if filed > as_of.astimezone(EASTERN_FIXED).date():
            after_cutoff += 1
            continue
        report = recent['reportDate'][i] or None
        entries.append({'accession': accession, 'form': form, 'amendment': form.endswith('/A'),
                        'report_period': date.fromisoformat(report) if report else None,
                        'filing_date': filed,
                        'acceptance_declared': (accepted[i] if len(accepted) == size and accepted[i] else None),
                        'cik': row['cik'], 'conformed_name': payload.get('name'),
                        'catalog_origin': 'submissions_recent',
                        'source_id': SUBMISSIONS, 'source_url': SUBMISSIONS_URL.format(cik=row['cik'])})
    files = (payload.get('filings') or {}).get('files') or []
    children = [{'name': f.get('name'), 'filing_from': f.get('filingFrom'), 'filing_to': f.get('filingTo'),
                 'filing_count': f.get('filingCount')} for f in files if isinstance(f, dict)]
    filed_dates = [date.fromisoformat(d) for d in recent['filingDate'] if d]
    coverage = {'recent_rows': size, 'recent_earliest_filing_date': min(filed_dates) if filed_dates else None,
                'recent_latest_filing_date': max(filed_dates) if filed_dates else None,
                'cataloged_forms': len(entries), 'excluded_after_cutoff': after_cutoff,
                'child_history_files_listed': children, 'child_history_files_fetched': 0,
                'child_history_files_note': 'Listed child files are separate documents outside the reviewed '
                                            'CIK{cik}.json scope; they are not fetched.'}
    return entries, coverage


HEADER_FIELDS = {
    'accession': r'ACCESSION NUMBER:\s*(\d{10}-\d{2}-\d{6})',
    'form': r'CONFORMED SUBMISSION TYPE:\s*([0-9A-Z/-]+)',
    'report_period': r'CONFORMED PERIOD OF REPORT:\s*(\d{8})',
    'filing_date': r'FILED AS OF DATE:\s*(\d{8})',
    'acceptance': r'<ACCEPTANCE-DATETIME>\s*(\d{14})',
}


def parse_index_header(text, row, accession, as_of):
    """One accession's SEC header: issuer identity must match the configured CIK and name."""
    body = html.unescape(re.sub(r'<(?!ACCEPTANCE-DATETIME)[^>]*>', ' ', html.unescape(text)))
    found = {k: (re.search(p, body).group(1) if re.search(p, body) else None) for k, p in HEADER_FIELDS.items()}
    if found['accession'] != accession:
        raise CatalogError('header_accession_mismatch', 'Header accession differs from the requested accession')
    if not found['form'] or not found['filing_date']:
        raise CatalogError('header_fields_missing', 'Header lacks submission type or filing date')
    ciks = re.findall(r'CENTRAL INDEX KEY:\s*(\d{10})', body)
    names = re.findall(r'COMPANY CONFORMED NAME:\s*([^\n]+?)\s*(?:\n|CENTRAL INDEX KEY)', body)
    pairs = list(zip(ciks, names))
    match = [(c, n.strip()) for c, n in pairs if c == row['cik']]
    if not match:
        raise CatalogError('issuer_cik_not_a_filer', 'Configured issuer CIK is not a filer of this accession')
    if not _name_matches(row, match[0][1]):
        raise CatalogError('issuer_name_mismatch_at_filing', 'Header issuer name does not match the configured issuer')
    filed = datetime.strptime(found['filing_date'], '%Y%m%d').date()
    if filed > as_of.astimezone(EASTERN_FIXED).date():
        return None
    stamp = found['acceptance']
    return {'accession': accession, 'form': found['form'], 'amendment': found['form'].endswith('/A'),
            'report_period': datetime.strptime(found['report_period'], '%Y%m%d').date() if found['report_period'] else None,
            'filing_date': filed,
            'acceptance_declared': (f'{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[8:10]}:{stamp[10:12]}:{stamp[12:]}'
                                    ' (EDGAR header, timezone not declared)') if stamp else None,
            'cik': row['cik'], 'conformed_name': match[0][1], 'catalog_origin': 'archive_index_header',
            'source_id': ARCHIVES,
            'source_url': HEADER_URL.format(cik_int=int(row['cik']), acc_nodash=accession.replace('-', ''), accession=accession)}


def cited_accessions(research_input, ticker):
    """Accessions cited by the reviewed financial evidence of the issuer (path or record id)."""
    from .retrospective import accession_of
    out = set()
    for e in research_input['evidence']:
        if e.get('entity_id') == ticker and e.get('kind') == 'financial':
            acc = accession_of(e.get('source_record_id'), e.get('article_url') or e.get('source_url'))
            if acc:
                out.add(acc)
    return sorted(out)


def _require_open(registry, source_id, url):
    snap = registry.snapshot(source_id)
    if not snap['source_reviewed'] or not snap['internal_use_allowed']:
        raise ValueError(f'{source_id}: not reviewed and open for internal use; catalog stays closed')
    if not registry.get(source_id).covers_endpoint(url):
        raise CatalogError('outside_reviewed_scope', f'{source_id}: {url} is outside the reviewed endpoint scope')
    return snap


def build(ticker, research_input, *, registry, client, as_of, now=None, max_headers=MAX_HEADER_LOOKUPS):
    """Fetch the base submissions file and missing accession headers; return normalized metadata."""
    row = issuer_row(ticker)
    url = SUBMISSIONS_URL.format(cik=row['cik'])
    sub_snap = _require_open(registry, SUBMISSIONS, url)
    if sub_snap['store_raw_response'] or sub_snap['store_response_hash']:
        raise ValueError('Catalog builder assumes raw/hash retention stays closed; review the registry first')
    payload, _ = client.get_json(url)
    entries, coverage = parse_submissions(payload, row, as_of)
    del payload  # never persisted
    known = {e['accession'] for e in entries}
    cited = cited_accessions(research_input, ticker)
    lookups = []
    missing = [a for a in cited if a not in known]
    for accession in missing[:max_headers]:
        header_url = HEADER_URL.format(cik_int=int(row['cik']), acc_nodash=accession.replace('-', ''), accession=accession)
        try:
            _require_open(registry, ARCHIVES, header_url)
            raw, _ = client.fetch(header_url, 'text/html')
            entry = parse_index_header(raw.decode('utf-8', errors='replace'), row, accession, as_of)
            del raw
        except CatalogError as exc:
            # Identity ambiguity is reported, never relaxed: the accession stays unverified.
            lookups.append({'accession': accession, 'status': 'failed', 'error_code': exc.code})
            continue
        except Exception as exc:  # exception text may echo response content; keep the type only
            lookups.append({'accession': accession, 'status': 'failed', 'error_code': 'retrieval_or_parse_failed',
                            'error_type': type(exc).__name__})
            continue
        if entry is None:
            lookups.append({'accession': accession, 'status': 'filed_after_cutoff'})
            continue
        entries.append(entry)
        lookups.append({'accession': accession, 'status': 'retrieved'})
    for accession in missing[max_headers:]:
        lookups.append({'accession': accession, 'status': 'not_attempted_bound_exceeded'})
    entries.sort(key=lambda e: (e['filing_date'], e['accession']))
    covered = {e['accession'] for e in entries}
    return {'schema_version': '1', 'entity_id': ticker, 'cik': row['cik'], 'as_of': as_of,
            'retrieved_at': now or datetime.now(timezone.utc),
            'sources': [SUBMISSIONS, ARCHIVES], 'entries': entries,
            'cited_accessions': len(cited), 'cited_accessions_cataloged': sum(a in covered for a in cited),
            'cited_accessions_uncataloged': sorted(a for a in cited if a not in covered),
            'coverage': coverage, 'header_lookups': lookups,
            'retention': 'normalized metadata only; no raw bodies, no response hashes, no contact data',
            'acceptance_note': 'Acceptance timestamps are recorded as declared; timezone semantics are not '
                               'independently verified, so they never move any clock or cutoff earlier.'}


def attach(research_input, catalog):
    """A new research input carrying the catalog; evidence, request and permissions are unchanged."""
    # Issuer, cutoff and entry dates are re-validated by ResearchInput.valid_catalog.
    if catalog['entity_id'] != research_input['request'].get('ticker'):
        raise ValueError('Catalog issuer differs from the research request')
    if research_input.get('filing_catalog') is not None:
        raise ValueError('Research input already carries a filing catalog; build from the reviewed input')
    return {**research_input, 'filing_catalog': catalog}


def index(catalog):
    """Accession → entry, plus the first non-amended periodic filing for each reporting period."""
    if not catalog:
        return {}, {}
    entries = catalog.entries if hasattr(catalog, 'entries') else catalog['entries']
    get = (lambda e, k: getattr(e, k)) if entries and not isinstance(entries[0], dict) else (lambda e, k: e[k])
    by_acc, first = {}, {}
    for e in entries:
        row = {k: get(e, k) for k in ('accession', 'form', 'amendment', 'report_period', 'filing_date', 'cik',
                                      'conformed_name', 'catalog_origin', 'acceptance_declared')}
        by_acc[row['accession']] = row
        base = row['form'].removesuffix('/A')
        if base in PERIODIC and not row['amendment'] and row['report_period']:
            key = row['report_period']
            if key not in first or (row['filing_date'], row['accession']) < (first[key]['filing_date'], first[key]['accession']):
                first[key] = row
    return by_acc, first


def classify(acc, period_end, preserved_clock, catalog_index, issuer_cik):
    """Exact vintage of one fact from its accession; returns (vintage, basis, verified_availability)."""
    by_acc, first = catalog_index
    entry = by_acc.get(acc) if acc else None
    if entry is None:
        return None
    if entry['cik'] != issuer_cik:
        return {'vintage': 'catalog_identity_mismatch', 'basis': 'catalog CIK differs from issuer', 'verified_availability': False,
                'entry': entry}
    # Acceptance is not public availability. Use a conservative end-of-filing-day
    # bound, and require an acceptance record rather than inferring one.
    if preserved_clock is None or preserved_clock < filing_day_upper_bound(entry['filing_date']):
        return {'vintage': 'catalog_clock_conflict', 'verified_availability': False, 'entry': entry,
                'basis': f'preserved clock precedes the catalog filing date {entry["filing_date"]}'}
    base = entry['form'].removesuffix('/A')
    period = entry['report_period']
    if base in PERIODIC and period:
        if period_end == period:
            if entry['amendment']:
                vintage, why = 'verified_amendment', f'{entry["form"]} amends the {period} report'
            elif first.get(period, {}).get('accession') == acc:
                vintage, why = 'verified_original', f'first non-amended {entry["form"]} for report period {period}'
            else:
                vintage, why = 'verified_later_periodic_filing', f'another non-amended filing reported {period} earlier'
        elif period_end < period:
            vintage, why = 'verified_comparative', f'comparative period inside the {period} {entry["form"]}'
        else:
            vintage, why = 'catalog_period_conflict', f'fact period {period_end} is after the filing report period {period}'
            return {'vintage': vintage, 'basis': why, 'verified_availability': False, 'entry': entry}
    else:
        vintage, why = 'verified_filing_period_role_unverified', (
            f'{entry["form"]} identity and filing date verified; the form header does not identify the fiscal '
            'reporting period, so original-versus-comparative status is not established')
    try:
        accepted_day = datetime.fromisoformat((entry.get('acceptance_declared') or '')[:19]).date()
        acceptance_recorded = accepted_day <= entry['filing_date']
    except ValueError:
        acceptance_recorded = False
    return {'vintage': vintage, 'basis': why,
            'verified_availability': acceptance_recorded,
            'availability_basis': 'conservative_filing_day_upper_bound_not_first_public_timestamp', 'entry': entry}
