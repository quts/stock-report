"""Historical filing dataset and walk-forward candidate retrospectives.

Two retrospectives are kept completely separate:

* `point_in_time`: each origin uses only versions preserved with a known clock before the
  origin's earliest preserved version (cutoff), and only if that cutoff precedes the end of the
  first target quarter. The clock is either a source-declared publication date or an observed
  availability upper bound; the two are never conflated, and no date is ever changed. The cutoff
  rule is shared with the full-forecast backtest (`backtesting.origin_release`).
* `revised_data`: today's values truncated at each origin. It answers "how did the rules behave
  on the data we now hold", never "what was knowable then"; it cannot promote a candidate.

Vintage status comes from an auditable filing catalog when the research input carries one
(`filing_catalog`: accession, issuer CIK, form, amendment flag, report period and filing date
from reviewed SEC indexes). Without a catalog entry the status stays an *inference* from the
available subset of each accession and from publication lag, and is never called verified.
Verified availability (the filing identity and date are catalog-confirmed) and verified
original status are separate: an amendment or comparative that demonstrably existed before the
cutoff is eligible, but it is labelled with its exact vintage.

Every candidate is scored against the baselines on identical (origin, horizon) keys and
identical actual evidence, separately for the raw rule and for the guidance-overlaid method that
is actually deployed, and separately for all preserved-clock origins and for catalog-verified
origins. Results are descriptive; sparse overlapping samples establish no statistical
reliability or forecasting superiority.
"""
import re
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import mean, median
from . import fin_series, filing_catalog
from .backtesting import origin_release, snapshot
from .candidates import BASELINES, CANDIDATES, DEFAULT_WINDOWS, min_history, overlay_guidance, path
from .policy import reportable

# Statutory filing windows (10-Q 40-45 days, 10-K 60-90 days; Taiwan Securities and Exchange Act
# Article 36: quarterly 45 days, annual three months). Used only to label an inference: a later
# first appearance may be a late original, an amendment or a comparative, so it stays unknown.
STATUTORY_WINDOW_DAYS = 120
DIAGNOSTIC_METRICS = ('revenue', 'operating_income', 'net_income', 'diluted_eps', 'operating_cash_flow', 'capital_expenditure')
ACCESSION = re.compile(r'^(\d{10}-\d{2}-\d{6})')
ARCHIVE = re.compile(r'/Archives/edgar/data/\d+/(\d{18})/')
HORIZON = 4
METHODS = ('raw_rule', 'guidance_overlay')
COVERAGE_FLOOR = 0.8
VERIFIED_VINTAGES = ('verified_original', 'verified_amendment', 'verified_comparative',
                     'verified_later_periodic_filing', 'verified_filing_period_role_unverified')
# Training-window alternatives already used by the stability checks; evaluated out of sample here
# only to describe sensitivity, never to pick a window.
WINDOW_VARIANTS = (('drift', (2, 8)), ('trailing_mean_level', (1, 4)), ('seasonal_yoy_trend', (2,)))


def accession_of(record_id, url):
    m = ACCESSION.match(record_id or '')
    if m:
        return m.group(1)
    m = ARCHIVE.search(url or '')
    if m:
        s = m.group(1)
        return f'{s[:10]}-{s[10:12]}-{s[12:]}'
    return None


def accession(e):
    return accession_of(e.source_record_id, e.article_url or e.source_url)


def clock_of(e):
    """What the preserved timestamp actually is; observed availability is never a publication date."""
    if e.publication_basis == 'observed_availability':
        return 'observed_availability_upper_bound'
    if e.publication_basis == 'source_declared_date':
        return 'source_publication_date_end_of_day_upper_bound'
    return 'source_publication_timestamp'


def _issuer_cik(entity_id):
    try:
        return filing_catalog.issuer_row(entity_id)['cik']
    except Exception:
        return None


def _catalog_summary(data, entity_id, rows):
    catalog = getattr(data, 'filing_catalog', None)
    if catalog is None or catalog.entity_id != entity_id:
        return {'status': 'absent', 'reason': 'research input carries no reviewed filing catalog; vintages stay inferred',
                'entries': 0}
    cited = {r['accession'] for r in rows if r['accession']}
    known = {e.accession for e in catalog.entries}
    forms = Counter(e.form for e in catalog.entries)
    return {'status': 'present', 'entries': len(catalog.entries), 'forms': dict(forms),
            'cik': catalog.cik, 'retrieved_at': catalog.retrieved_at, 'as_of': catalog.as_of,
            'sources': catalog.sources, 'cited_accessions_in_dataset': len(cited),
            'cited_accessions_cataloged': len(cited & known),
            'cited_accessions_uncataloged': sorted(cited - known),
            'origin_counts': dict(Counter(e.catalog_origin for e in catalog.entries)),
            'header_lookups': dict(Counter(h.get('status') for h in catalog.header_lookups)),
            'coverage': catalog.coverage, 'acceptance_note': catalog.acceptance_note}


def filing_dataset(data, entity_id):
    """Every reviewed financial fact with its filing identity, clock basis and vintage."""
    facts = [e for e in data.evidence if e.entity_id == entity_id and e.kind == 'financial' and e.statement_kind == 'fact'
             and e.financial_metric and e.value is not None and e.period_end and reportable(e, data.request)]
    catalog = getattr(data, 'filing_catalog', None)
    cat_index = filing_catalog.index(catalog) if catalog is not None and catalog.entity_id == entity_id else ({}, {})
    cik = _issuer_cik(entity_id)
    groups = defaultdict(list)
    for e in facts:
        acc = accession(e)
        key = ('accession', acc) if acc else ('document', e.article_url or e.source_url, e.published_at.date().isoformat())
        groups[key].append(e)
    rows = []
    for key, items in groups.items():
        # The latest period present in the *available subset* of the accession, not a verified header.
        subset_end = max(e.period_end for e in items)
        periods = len({x.period_end for x in items})
        for e in items:
            lag = (e.published_at.date() - e.period_end).days
            acc = key[1] if key[0] == 'accession' else None
            verified = filing_catalog.classify(acc, e.period_end, e.published_at, cat_index, cik) if acc else None
            if verified is not None:
                vintage, why = verified['vintage'], verified['basis']
            elif key[0] != 'accession':
                vintage, why = 'unknown', 'no accession: the document reporting period cannot be identified'
            elif e.period_end < subset_end:
                vintage, why = 'inferred_comparative', 'earlier than another period presented in the same accession'
            elif periods == 1:
                vintage, why = 'unknown', 'only one period of this accession is preserved; its reporting period is not identified'
            elif lag > STATUTORY_WINDOW_DAYS:
                vintage, why = 'unknown', f'latest period in the accession but first preserved {lag} days after period end: late original, amendment or restatement'
            else:
                vintage, why = 'inferred_original', 'latest period in the preserved accession subset within statutory windows; header not verified'
            entry = (verified or {}).get('entry')
            rows.append({'evidence_id': e.evidence_id, 'metric': e.financial_metric, 'period_start': e.period_start,
                         'period_end': e.period_end, 'period_basis': e.period_basis, 'value': e.value, 'unit': e.unit,
                         'currency': e.currency, 'accounting_basis': e.accounting_basis or 'unspecified',
                         'accession': acc, 'source_record_id': e.source_record_id,
                         'license_source_id': e.license_source_id, 'source_url': e.article_url or e.source_url,
                         'preserved_version_clock': e.published_at, 'publication_basis': e.publication_basis,
                         'clock_basis': clock_of(e), 'source_publication_timestamp_known': e.publication_basis == 'source_declared_timestamp',
                         'source_publication_date_known': e.publication_basis != 'observed_availability',
                         'accession_subset_latest_period_end': subset_end, 'filing_lag_days': lag,
                         'vintage': vintage, 'vintage_basis': why, 'original_verified': vintage == 'verified_original',
                         'availability_verified': bool(verified and verified['verified_availability']),
                         'availability_verification_basis': (verified or {}).get('availability_basis'),
                         'catalog': None if entry is None else {k: entry[k] for k in (
                             'form', 'amendment', 'report_period', 'filing_date', 'acceptance_declared', 'catalog_origin')}})
    rows.sort(key=lambda r: (r['metric'], r['period_end'], r['period_basis'] or '', r['preserved_version_clock']))
    coverage = {}
    for metric in sorted({r['metric'] for r in rows}):
        by_period = defaultdict(list)
        for r in rows:
            if r['metric'] == metric:
                by_period[(r['period_start'], r['period_end'], r['period_basis'])].append(r)
        coverage[metric] = {'windows': len(by_period),
                            'with_verified_original': sum(any(r['vintage'] == 'verified_original' for r in v) for v in by_period.values()),
                            'with_verified_availability': sum(any(r['availability_verified'] for r in v) for v in by_period.values()),
                            'with_inferred_original': sum(any(r['vintage'] == 'inferred_original' for r in v) for v in by_period.values()),
                            'inferred_comparative_only': sum(all(r['vintage'] == 'inferred_comparative' for r in v) for v in by_period.values()),
                            'unknown_only': sum(all(r['vintage'] == 'unknown' for r in v) for v in by_period.values()),
                            'multiple_versions_preserved': sum(len(v) > 1 for v in by_period.values())}
    clocks = Counter(r['clock_basis'] for r in rows)
    catalog_info = _catalog_summary(data, entity_id, rows)
    return {'entity_id': entity_id, 'rows': rows, 'coverage': coverage, 'vintage_counts': dict(Counter(r['vintage'] for r in rows)),
            'clock_basis_counts': dict(clocks),
            'source_publication_timestamp_missing': sum(not r['source_publication_timestamp_known'] for r in rows),
            'verified_original_count': sum(r['original_verified'] for r in rows),
            'verified_availability_count': sum(r['availability_verified'] for r in rows),
            'catalog': catalog_info,
            'classification_rule': {
                'verified_original': 'catalog: first non-amended periodic filing whose report period equals the fact period',
                'verified_amendment': 'catalog: amended periodic filing whose report period equals the fact period',
                'verified_comparative': 'catalog: periodic filing whose report period is later than the fact period',
                'verified_filing_period_role_unverified': 'catalog: filing identity and date verified; the form (e.g. 6-K/8-K) does not declare the fiscal period',
                'inferred_original': 'no catalog entry: latest period among >=2 preserved periods of one accession, first preserved within '
                                     f'{STATUTORY_WINDOW_DAYS} days; reportPeriod header not verified',
                'inferred_comparative': 'no catalog entry: an earlier period inside an accession that also presents a later period',
                'unknown': 'no catalog entry and no accession, a single preserved period, or a late first appearance'},
            'acquisition': {'new_original_filings_acquired': False,
                            'scope': 'this diagnostic function; upstream acquisition is recorded in the run data-review',
                            'note': 'Built only from the reviewed evidence bundle and, when present, the reviewed filing '
                                    'catalog (metadata only); no original filing values were fetched or reconstructed.'}}


def _index(dataset):
    out = defaultdict(list)
    for r in dataset['rows']:
        out[(r['metric'], r['period_end'])].append(r)
    return out


def _by_evidence(dataset):
    return {r['evidence_id']: r for r in dataset['rows']}


def _period_status(index, metric, end, cutoff=None):
    rows = index.get((metric, end), [])
    if not rows:
        return {'status': 'absent_from_evidence'}
    first = min(rows, key=lambda r: r['preserved_version_clock'])
    out = {'earliest_preserved_clock': first['preserved_version_clock'], 'earliest_clock_basis': first['clock_basis'],
           'earliest_vintage': first['vintage'], 'earliest_evidence_id': first['evidence_id'],
           'vintages': sorted({r['vintage'] for r in rows}), 'evidence_ids': sorted(r['evidence_id'] for r in rows)}
    if cutoff is not None:
        out['status'] = 'available_at_cutoff' if first['preserved_version_clock'] < cutoff else 'preserved_only_after_cutoff'
    else:
        out['status'] = 'preserved'
    return out


def _vintages(ids, by_id):
    return sorted({by_id[i]['vintage'] for i in ids if i in by_id})


def _verified(ids, by_id):
    return bool(ids) and all(i in by_id and by_id[i]['availability_verified'] for i in ids)


def _errors(values, targets, by_id=None):
    by_id = by_id or {}
    errors = {}
    for h, (value, actual) in enumerate(zip(values, targets), start=1):
        if actual is None or value is None:
            continue
        errors[str(h)] = {'predicted': value, 'actual': actual['value'],
                          'error_percent': (value - actual['value']) / abs(actual['value']) * 100 if actual['value'] else None,
                          'actual_evidence_ids': sorted(actual['evidence_ids']),
                          'actual_vintages': _vintages(actual['evidence_ids'], by_id)}
    if len(targets) == HORIZON and all(targets) and all(v is not None for v in values):
        total = sum(t['value'] for t in targets)
        ids = sorted({i for t in targets for i in t['evidence_ids']})
        errors['ntm'] = {'predicted': sum(values), 'actual': total,
                         'error_percent': (sum(values) - total) / abs(total) * 100 if total else None,
                         'actual_evidence_ids': ids, 'actual_vintages': _vintages(ids, by_id)}
    return errors


def _targets(points, i):
    out, last = [], points[i]
    for p in points[i + 1:i + 1 + HORIZON]:
        if p['start'] != last['end'] + timedelta(days=1):
            break
        out.append(p)
        last = p
    return out


def _pad(targets):
    return targets + [None] * (HORIZON - len(targets))


def _stats(errs):
    pct = [e['error_percent'] for e in errs if e['error_percent'] is not None]
    return {'mae': mean(abs(e['predicted'] - e['actual']) for e in errs) if errs else None,
            'mape_percent': mean(abs(v) for v in pct) if pct else None,
            'bias_percent': mean(pct) if pct else None,
            'median_ape_percent': median(abs(v) for v in pct) if pct else None}


def matched_pairs(a, b, keys):
    """Candidate-versus-baseline statistics on identical keys only."""
    if not keys:
        return {'samples': 0, 'candidate': _stats([]), 'baseline': _stats([]), 'mape_improvement_pp': None,
                'mae_improvement': None, 'candidate_lower_error': 0, 'baseline_lower_error': 0, 'ties': 0}
    ca, ba = _stats([a[k] for k in keys]), _stats([b[k] for k in keys])
    diffs = [abs(b[k]['error_percent']) - abs(a[k]['error_percent']) for k in keys]
    return {'samples': len(keys), 'candidate': ca, 'baseline': ba,
            'mape_improvement_pp': ba['mape_percent'] - ca['mape_percent'],
            'mae_improvement': ba['mae'] - ca['mae'],
            'median_pair_improvement_pp': median(diffs),
            'candidate_lower_error': sum(d > 0 for d in diffs), 'baseline_lower_error': sum(d < 0 for d in diffs),
            'ties': sum(d == 0 for d in diffs)}


def _keyed(rows, method, verified_only=False):
    keyed = defaultdict(dict)
    for row in rows:
        for name, result in row['candidates'].items():
            if verified_only and not result.get('inputs_verified'):
                continue
            if verified_only and method == 'guidance_overlay' and not (
                    (result.get('methods') or {}).get(method) or {}).get('guidance_inputs_verified', False):
                continue
            for h, err in ((((result.get('methods') or {}).get(method) or {}).get('errors')) or {}).items():
                if err['error_percent'] is not None:
                    keyed[name][(str(row['origin']), str(h))] = err
    return keyed


def _score(rows, method='raw_rule', verified_only=False):
    """Per candidate and pairwise against every baseline, on identical keys and actual evidence only."""
    keyed = _keyed(rows, method, verified_only)
    ntm_origins = sum(1 for r in rows if len(r['targets']) == HORIZON and (not verified_only or r.get('origin_availability_verified')))
    per = {}
    for name in CANDIDATES:
        items = keyed.get(name, {})
        horizons = {}
        for h in [str(x) for x in range(1, HORIZON + 1)] + ['ntm']:
            errs = [e for (o, k), e in items.items() if k == h]
            horizons[h] = {'samples': len(errs), **_stats(errs)}
        per[name] = horizons
    pairwise = {}
    for name in CANDIDATES:
        pairwise[name] = {}
        for base in BASELINES:
            if base == name:
                continue
            a, b = keyed.get(name, {}), keyed.get(base, {})
            common = sorted(k for k in a.keys() & b.keys()
                            if a[k]['actual'] == b[k]['actual'] and a[k]['actual_evidence_ids'] == b[k]['actual_evidence_ids'])
            quarterly = [k for k in common if k[1] != 'ntm']
            ntm = [k for k in common if k[1] == 'ntm']
            q, n = matched_pairs(a, b, quarterly), matched_pairs(a, b, ntm)
            pairwise[name][base] = {'method': method, 'verified_only': verified_only,
                                    'matched_quarter_samples': len(quarterly), 'matched_ntm_samples': len(ntm),
                                    'ntm_origins_with_complete_targets': ntm_origins,
                                    'ntm_coverage': len(ntm) / ntm_origins if ntm_origins else 0.0,
                                    'matched_origins': sorted({k[0] for k in common}),
                                    'candidate_quarter_mape': q['candidate']['mape_percent'], 'baseline_quarter_mape': q['baseline']['mape_percent'],
                                    'candidate_ntm_mape': n['candidate']['mape_percent'], 'baseline_ntm_mape': n['baseline']['mape_percent'],
                                    'candidate_lower_error_count': q['candidate_lower_error'],
                                    'quarterly': q, 'ntm': n}
    applicable_all = [n for n in CANDIDATES if keyed.get(n)]
    shared = set.intersection(*(set(keyed[n]) for n in applicable_all)) if applicable_all else set()
    common_all = {n: {'samples': len(shared), **_stats([keyed[n][k] for k in shared])} for n in applicable_all}
    return {'method': method, 'verified_only': verified_only, 'per_candidate': per, 'pairwise': pairwise,
            'common_all_candidates': common_all, 'common_keys': sorted(f'{o}@h{h}' for o, h in shared)}


def revised_data(data, entity_id):
    full = fin_series.build(data, entity_id)
    points = full['quarters'].get('revenue', [])
    rows = []
    for i in range(len(points) - 1):
        targets = _targets(points, i)
        if not targets:
            continue
        row = {'origin': points[i]['end'], 'targets': [t['end'] for t in targets], 'candidates': {}}
        for name in CANDIDATES:
            p = path(name, points[:i + 1])
            row['candidates'][name] = {'methods': {'raw_rule': {'errors': _errors(p['values'], _pad(targets)) if p['values'] else None,
                                                                'not_applicable_reason': p['reason']}}}
        rows.append(row)
    return {'method': 'revised_data_not_point_in_time', 'evaluated_method': 'raw_rule_without_guidance',
            'rows': rows, 'scores': _score(rows), 'promotes_candidates': False,
            'limitation': '使用目前持有的最新數值，依期間截斷；不是當時可得資料。只評估未疊加指引的原始規則。樣本少且重疊，只作探索。'}


def _historical_guidance(observed, entity_id, targets, cutoff):
    """Guidance preserved before the cutoff, applied only to its own target quarter; no historical FX is invented."""
    from .forecast import guidance_records
    records = [g for g in guidance_records(observed, entity_id) if g['published'] < cutoff]
    windows = [{'start': t['start'], 'end': t['end'], 'prior': {'currency': t['currency']}} for t in targets]
    return records, windows


def _window_sensitivity(rows):
    """Out-of-sample effect of each alternative training window versus the default, identical keys."""
    out = {}
    for name, windows in WINDOW_VARIANTS:
        default = {}
        for row in rows:
            errs = ((((row['candidates'].get(name) or {}).get('methods') or {}).get('raw_rule') or {}).get('errors')) or {}
            for h, e in errs.items():
                if e['error_percent'] is not None:
                    default[(str(row['origin']), h)] = e
        for w in windows:
            variant = {}
            for row in rows:
                for h, e in ((row.get('window_variants') or {}).get(f'{name}@{w}') or {}).items():
                    if e['error_percent'] is not None:
                        variant[(str(row['origin']), h)] = e
            keys = sorted(k for k in variant.keys() & default.keys() if variant[k]['actual'] == default[k]['actual'])
            q, n = [k for k in keys if k[1] != 'ntm'], [k for k in keys if k[1] == 'ntm']
            # "candidate" is the variant window and "baseline" the published default window.
            out[f'{name}@{w}'] = {'default_window': DEFAULT_WINDOWS[name], 'variant_window': w,
                                  'quarterly': matched_pairs(variant, default, q), 'ntm': matched_pairs(variant, default, n)}
    return {'variants': out, 'note': 'Descriptive only: alternative windows are never selected from these errors.'}


def point_in_time(data, entity_id, dataset=None):
    dataset = dataset or filing_dataset(data, entity_id)
    index, by_id = _index(dataset), _by_evidence(dataset)
    full = fin_series.build(data, entity_id)
    points = full['quarters'].get('revenue', [])
    catalog = getattr(data, 'filing_catalog', None)
    catalog_by_period = defaultdict(list)
    if catalog is not None and catalog.entity_id == entity_id:
        for f in catalog.entries:
            if f.report_period and f.form.removesuffix('/A') in filing_catalog.PERIODIC + ('6-K',):
                catalog_by_period[f.report_period].append(f.model_dump())
    cited_accessions = {r['accession'] for r in dataset['rows'] if r['accession']}
    rows, causes, violations = [], Counter(), []
    for i in range(len(points) - 1):
        origin = points[i]['end']
        targets = _targets(points, i)
        row = {'origin': origin, 'targets': [t['end'] for t in targets], 'candidates': {}, 'root_causes': [],
               'origin_metric_status': {m: _period_status(index, m, origin) for m in DIAGNOSTIC_METRICS}}
        rel = origin_release(data, entity_id, origin)
        release = rel['clock']
        origin_row = by_id.get(rel['evidence_id']) or {}
        row['origin_clock'] = {'earliest_preserved_clock': release, 'earliest_clock_basis': origin_row.get('clock_basis'),
                               'earliest_vintage': origin_row.get('vintage'), 'earliest_evidence_id': rel['evidence_id'],
                               'availability_verified': bool(origin_row.get('availability_verified')),
                               'catalog': origin_row.get('catalog')}
        row['origin_vintage'] = origin_row.get('vintage')
        row['origin_availability_verified'] = bool(origin_row.get('availability_verified'))
        if not targets:
            row['root_causes'].append({'code': 'no_contiguous_target_quarter'})
        if release is None:
            row['root_causes'].append({'code': 'origin_quarter_absent_from_evidence'})
        elif release + timedelta(microseconds=1) >= data.request.research_as_of:
            row['root_causes'].append({'code': 'origin_preserved_only_after_research_cutoff'})
        elif targets and release.date() >= targets[0]['end']:
            # The only preserved versions appeared after the first target quarter ended.
            row['root_causes'].append({'code': 'origin_preserved_only_after_first_target_end',
                                       'detail': {'earliest_clock': release, 'clock_basis': origin_row.get('clock_basis'),
                                                  'vintage': origin_row.get('vintage'),
                                                  'first_target_end': targets[0]['end']}})
        if row['root_causes']:
            if targets and catalog_by_period:
                # Filings the catalog shows existed for this period before the first target ended, but
                # whose values were never collected: an acquisition gap, not a model limitation.
                row['uncollected_catalog_candidates'] = [
                    {k: f[k] for k in ('accession', 'form', 'report_period', 'filing_date')}
                    for f in catalog_by_period.get(origin, []) if f['filing_date'] < targets[0]['end']
                    and f['accession'] not in cited_accessions]
                if row['uncollected_catalog_candidates']:
                    causes['catalog_lists_uncollected_origin_period_filing'] += 1
            for c in row['root_causes']:
                causes[c['code']] += 1
            row['eligible'] = False
            rows.append(row)
            continue
        cutoff = release + timedelta(microseconds=1)
        row.update(eligible=True, information_cutoff=cutoff, cutoff_basis=origin_row.get('clock_basis'))
        observed = snapshot(data, origin, cutoff)
        visible = [p for p in fin_series.build(observed, entity_id)['quarters'].get('revenue', []) if p['end'] <= origin]
        row['visible_history'] = [p['end'] for p in visible]
        used = [e for e in observed.evidence if e.kind == 'financial' or e.statement_kind == 'management_guidance']
        row['max_input_clock'] = max((e.published_at for e in used), default=None)
        if row['max_input_clock'] is not None and row['max_input_clock'] >= cutoff:
            violations.append(str(origin))
        if not visible or visible[-1]['end'] != origin:
            row['root_causes'].append({'code': 'origin_quarter_not_derivable_at_cutoff',
                                       'detail': 'single quarter requires an earlier cumulative window that was not yet preserved'})
        guidance, windows = _historical_guidance(observed, entity_id, targets, cutoff)
        row['historical_guidance_records'] = len(guidance)
        for name in CANDIDATES:
            need = min_history(name)
            required = points[max(0, i - need + 1):i + 1]
            if len(required) < need or not fin_series.contiguous_tail(required, need):
                row['candidates'][name] = {'methods': {}, 'not_applicable_reason': 'insufficient_training_history_in_collected_evidence',
                                           'required_quarters': need}
                causes[f'{name}:insufficient_training_history_in_collected_evidence'] += 1
                continue
            missing = [{'period_end': p['end'], **_period_status(index, 'revenue', p['end'], cutoff)}
                       for p in required if p['end'] not in {v['end'] for v in visible}]
            if missing:
                row['candidates'][name] = {'methods': {}, 'not_applicable_reason': 'training_period_unavailable_at_cutoff',
                                           'missing_periods': missing}
                for m in missing:
                    causes[f'{name}:training_period_{m["status"]}:{m.get("earliest_vintage")}'] += 1
                continue
            p = path(name, visible)
            train = visible[-need:]
            train_ids = sorted({i for t in train for i in t['evidence_ids']})
            methods = {'raw_rule': {'errors': _errors(p['values'], _pad(targets), by_id) if p['values'] else None,
                                    'not_applicable_reason': p['reason']}}
            if p['values']:
                guided, used_g, rejected = overlay_guidance(windows, p['values'][:len(targets)], guidance)
                if used_g:
                    methods['guidance_overlay'] = {'errors': _errors([q['value'] for q in guided], _pad(targets), by_id),
                                                   'guidance_inputs_verified': _verified([i for u in used_g for i in u['evidence_ids']], by_id),
                                                   'guidance_evidence_ids': [i for u in used_g for i in u['evidence_ids']]}
                else:
                    methods['guidance_overlay'] = {'errors': None, 'not_applicable_reason':
                                                   'no_preserved_historical_guidance_for_target_quarters' if not rejected
                                                   else 'historical_guidance_rejected_' + rejected[0]['reason']}
            row['candidates'][name] = {'methods': methods, 'training_window': [t['end'] for t in train],
                                       'training_vintages': _vintages(train_ids, by_id),
                                       'inputs_verified': row['origin_availability_verified'] and _verified(train_ids, by_id)}
        row['window_variants'] = {}
        for name, alternatives in WINDOW_VARIANTS:
            for w in alternatives:
                p = path(name, visible, window=w)
                if p['values']:
                    row['window_variants'][f'{name}@{w}'] = _errors(p['values'], _pad(targets), by_id)
        row['sample_class'] = ('catalog_verified' if any(c.get('inputs_verified') for c in row['candidates'].values())
                               else 'preserved_clock_only')
        for c in row['root_causes']:
            causes[c['code']] += 1
        rows.append(row)
    eligible = [r for r in rows if r.get('eligible')]
    scores = {m: _score(eligible, m) for m in METHODS}
    verified_scores = {m: _score(eligible, m, verified_only=True) for m in METHODS}
    reason = None
    if not eligible:
        top = Counter(c['code'] for r in rows for c in r['root_causes']).most_common(1)
        reason = top[0][0] if top else 'no_origins'
    initialized = [r for r in eligible if any(((c.get('methods') or {}).get('raw_rule') or {}).get('errors') is not None
                                               for c in r['candidates'].values())]
    funnel = {'catalog_entries': dataset['catalog'].get('entries', 0),
              'catalog_status': dataset['catalog']['status'],
              'financial_periods': len(points),
              'origins': len(rows),
              'origins_with_complete_ntm_targets': sum(len(r['targets']) == HORIZON for r in rows),
              'preserved_clock_eligible_origins': len(eligible),
              'initialized_prediction_origins': len(initialized),
              'catalog_verified_origins': sum(r['origin_availability_verified'] for r in eligible),
              'catalog_verified_original_origins': sum(r['origin_vintage'] == 'verified_original' for r in eligible),
              'verified_evaluated_ntm_samples': {n: verified_scores['raw_rule']['per_candidate'][n]['ntm']['samples'] for n in CANDIDATES},
              'preserved_clock_evaluated_ntm_samples': {n: scores['raw_rule']['per_candidate'][n]['ntm']['samples'] for n in CANDIDATES},
              'note': 'Each stage is counted separately; a catalog entry, a financial period or an initialized prediction is not an evaluated sample.'}
    return {'method': 'strict_point_in_time_earliest_preserved_version', 'rows': rows, 'scores': scores['raw_rule'],
            'scores_by_method': scores, 'pairwise_by_method': {m: s['pairwise'] for m, s in scores.items()},
            'verified_scores_by_method': verified_scores,
            'verified_pairwise_by_method': {m: s['pairwise'] for m, s in verified_scores.items()},
            'window_sensitivity': _window_sensitivity(eligible),
            'origin_vintage_counts': dict(Counter(r.get('origin_vintage') or 'absent' for r in eligible)),
            'funnel': funnel,
            'root_cause_counts': dict(causes), 'eligible_origins': len(eligible), 'origins': len(rows),
            'verified_origins': funnel['catalog_verified_origins'],
            'zero_valid_origin_reason': reason,
            'integrity': {'status': 'pass' if not violations else 'fail', 'violations': violations,
                          'rule': 'every input clock precedes the cutoff; cutoff precedes the first target quarter end'},
            'clock_note': 'Cutoffs use the earliest preserved version of the origin quarter (shared with the full-forecast '
                          'backtest): a source publication date or an observed-availability upper bound (conservative, not a '
                          'publication date). No version is redated; catalog acceptance times never move a cutoff earlier.',
            'actuals_basis': 'latest reviewed values for target quarters; matched pairs require identical actual evidence IDs, '
                             'and each error lists the actual vintages',
            'limitation': '起點只使用截止前已保留、時鐘已知的版本；觀察到的可得時間只是保守上限，不是原始公布時間。'
                          '原始申報身分只在有申報目錄核對時標為已驗證；其餘為推論。'}


def evaluate(data, entity_id):
    dataset = filing_dataset(data, entity_id)
    return {'filing_dataset': dataset, 'point_in_time': point_in_time(data, entity_id, dataset),
            'revised_data': revised_data(data, entity_id),
            'claims': 'none: retrospectives are descriptive; no statistical reliability or forecasting superiority is asserted'}
