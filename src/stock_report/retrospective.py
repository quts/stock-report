"""Historical filing dataset and walk-forward candidate retrospectives.

Two retrospectives are kept completely separate:

* `point_in_time`: each origin uses only versions preserved with a known clock before the
  origin's earliest preserved version (cutoff), and only if that cutoff precedes the end of the
  first target quarter. The clock is either a source-declared publication date or an observed
  availability upper bound; the two are never conflated, and no date is ever changed.
* `revised_data`: today's values truncated at each origin. It answers "how did the rules behave
  on the data we now hold", never "what was knowable then"; it cannot promote a candidate.

Original-versus-comparative status is *inferred* from the available subset of each accession and
from publication lag; no filing reportPeriod header is verified here, so no row is ever labelled
a verified original. Every candidate is scored against the baselines on identical (origin,
horizon) keys and actuals, separately for the raw rule and for the guidance-overlaid method that
is actually deployed. Results are descriptive; sparse overlapping samples establish no
statistical reliability or forecasting superiority.
"""
import re
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import mean
from . import fin_series
from .backtesting import snapshot
from .candidates import BASELINES, CANDIDATES, min_history, overlay_guidance, path
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


def accession(e):
    m = ACCESSION.match(e.source_record_id or '')
    if m:
        return m.group(1)
    m = ARCHIVE.search(e.article_url or e.source_url or '')
    if m:
        s = m.group(1)
        return f'{s[:10]}-{s[10:12]}-{s[12:]}'
    return None


def clock_of(e):
    """What the preserved timestamp actually is; observed availability is never a publication date."""
    if e.publication_basis == 'observed_availability':
        return 'observed_availability_upper_bound'
    if e.publication_basis == 'source_declared_date':
        return 'source_publication_date_end_of_day_upper_bound'
    return 'source_publication_timestamp'


def filing_dataset(data, entity_id):
    """Every reviewed financial fact with its filing identity, clock basis and inferred vintage."""
    facts = [e for e in data.evidence if e.entity_id == entity_id and e.kind == 'financial' and e.statement_kind == 'fact'
             and e.financial_metric and e.value is not None and e.period_end and reportable(e, data.request)]
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
            if key[0] != 'accession':
                vintage, why = 'unknown', 'no accession: the document reporting period cannot be identified'
            elif e.period_end < subset_end:
                vintage, why = 'inferred_comparative', 'earlier than another period presented in the same accession'
            elif periods == 1:
                vintage, why = 'unknown', 'only one period of this accession is preserved; its reporting period is not identified'
            elif lag > STATUTORY_WINDOW_DAYS:
                vintage, why = 'unknown', f'latest period in the accession but first preserved {lag} days after period end: late original, amendment or restatement'
            else:
                vintage, why = 'inferred_original', 'latest period in the preserved accession subset within statutory windows; header not verified'
            rows.append({'evidence_id': e.evidence_id, 'metric': e.financial_metric, 'period_start': e.period_start,
                         'period_end': e.period_end, 'period_basis': e.period_basis, 'value': e.value, 'unit': e.unit,
                         'currency': e.currency, 'accounting_basis': e.accounting_basis or 'unspecified',
                         'accession': key[1] if key[0] == 'accession' else None, 'source_record_id': e.source_record_id,
                         'license_source_id': e.license_source_id, 'source_url': e.article_url or e.source_url,
                         'preserved_version_clock': e.published_at, 'publication_basis': e.publication_basis,
                         'clock_basis': clock_of(e), 'source_publication_timestamp_known': e.publication_basis != 'observed_availability',
                         'accession_subset_latest_period_end': subset_end, 'filing_lag_days': lag,
                         'vintage': vintage, 'vintage_basis': why, 'original_verified': False})
    rows.sort(key=lambda r: (r['metric'], r['period_end'], r['period_basis'] or '', r['preserved_version_clock']))
    coverage = {}
    for metric in sorted({r['metric'] for r in rows}):
        by_period = defaultdict(list)
        for r in rows:
            if r['metric'] == metric:
                by_period[(r['period_start'], r['period_end'], r['period_basis'])].append(r)
        coverage[metric] = {'windows': len(by_period),
                            'with_inferred_original': sum(any(r['vintage'] == 'inferred_original' for r in v) for v in by_period.values()),
                            'inferred_comparative_only': sum(all(r['vintage'] == 'inferred_comparative' for r in v) for v in by_period.values()),
                            'unknown_only': sum(all(r['vintage'] == 'unknown' for r in v) for v in by_period.values()),
                            'multiple_versions_preserved': sum(len(v) > 1 for v in by_period.values())}
    clocks = Counter(r['clock_basis'] for r in rows)
    return {'entity_id': entity_id, 'rows': rows, 'coverage': coverage, 'vintage_counts': dict(Counter(r['vintage'] for r in rows)),
            'clock_basis_counts': dict(clocks),
            'source_publication_timestamp_missing': sum(not r['source_publication_timestamp_known'] for r in rows),
            'verified_original_count': 0,
            'classification_rule': {
                'inferred_original': 'latest period among >=2 preserved periods of one accession, first preserved within '
                                     f'{STATUTORY_WINDOW_DAYS} days; reportPeriod header not verified',
                'inferred_comparative': 'an earlier period inside an accession that also presents a later period',
                'unknown': 'no accession, a single preserved period, or a late first appearance (late original, amendment or comparative)'},
            'acquisition': {'new_original_filings_acquired': False,
                            'note': 'Built only from the reviewed evidence bundle; no original filing snapshot was fetched or reconstructed.'}}


def _index(dataset):
    out = defaultdict(list)
    for r in dataset['rows']:
        out[(r['metric'], r['period_end'])].append(r)
    return out


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


def _errors(values, targets):
    errors = {}
    for h, (value, actual) in enumerate(zip(values, targets), start=1):
        if actual is None or value is None:
            continue
        errors[str(h)] = {'predicted': value, 'actual': actual['value'],
                          'error_percent': (value - actual['value']) / abs(actual['value']) * 100 if actual['value'] else None,
                          'actual_evidence_ids': actual['evidence_ids']}
    if len(targets) == HORIZON and all(targets) and all(v is not None for v in values):
        total = sum(t['value'] for t in targets)
        errors['ntm'] = {'predicted': sum(values), 'actual': total,
                         'error_percent': (sum(values) - total) / abs(total) * 100 if total else None}
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


def _score(rows, method='raw_rule'):
    """Per candidate and pairwise against every baseline, on identical keys and actuals only."""
    keyed = defaultdict(dict)
    for row in rows:
        for name, result in row['candidates'].items():
            for h, err in ((((result.get('methods') or {}).get(method) or {}).get('errors')) or {}).items():
                if err['error_percent'] is not None:
                    keyed[name][(str(row['origin']), str(h))] = err
    ntm_origins = sum(1 for r in rows if len(r['targets']) == HORIZON)
    per = {}
    for name in CANDIDATES:
        items = keyed.get(name, {})
        horizons = {}
        for h in [str(x) for x in range(1, HORIZON + 1)] + ['ntm']:
            vals = [e['error_percent'] for (o, k), e in items.items() if k == h]
            horizons[h] = {'samples': len(vals), 'mape_percent': mean(abs(v) for v in vals) if vals else None,
                           'bias_percent': mean(vals) if vals else None}
        per[name] = horizons
    pairwise = {}
    for name in CANDIDATES:
        pairwise[name] = {}
        for base in BASELINES:
            if base == name:
                continue
            common = sorted(k for k in keyed.get(name, {}).keys() & keyed.get(base, {}).keys()
                            if keyed[name][k]['actual'] == keyed[base][k]['actual'])
            quarterly = [k for k in common if k[1] != 'ntm']
            ntm = [k for k in common if k[1] == 'ntm']
            def mape(group, keys):
                return mean(abs(keyed[group][k]['error_percent']) for k in keys) if keys else None
            pairwise[name][base] = {'method': method, 'matched_quarter_samples': len(quarterly), 'matched_ntm_samples': len(ntm),
                                    'ntm_origins_with_complete_targets': ntm_origins,
                                    'ntm_coverage': len(ntm) / ntm_origins if ntm_origins else 0.0,
                                    'matched_origins': sorted({k[0] for k in common}),
                                    'candidate_quarter_mape': mape(name, quarterly), 'baseline_quarter_mape': mape(base, quarterly),
                                    'candidate_ntm_mape': mape(name, ntm), 'baseline_ntm_mape': mape(base, ntm),
                                    'candidate_lower_error_count': sum(abs(keyed[name][k]['error_percent']) < abs(keyed[base][k]['error_percent']) for k in quarterly)}
    applicable_all = [n for n in CANDIDATES if keyed.get(n)]
    shared = set.intersection(*(set(keyed[n]) for n in applicable_all)) if applicable_all else set()
    common_all = {n: {'samples': len(shared), 'mape_percent': mean(abs(keyed[n][k]['error_percent']) for k in shared) if shared else None}
                  for n in applicable_all}
    return {'method': method, 'per_candidate': per, 'pairwise': pairwise, 'common_all_candidates': common_all,
            'common_keys': sorted(f'{o}@h{h}' for o, h in shared)}


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


def point_in_time(data, entity_id, dataset=None):
    dataset = dataset or filing_dataset(data, entity_id)
    index = _index(dataset)
    full = fin_series.build(data, entity_id)
    points = full['quarters'].get('revenue', [])
    rows, causes, violations = [], Counter(), []
    for i in range(len(points) - 1):
        origin = points[i]['end']
        targets = _targets(points, i)
        row = {'origin': origin, 'targets': [t['end'] for t in targets], 'candidates': {}, 'root_causes': [],
               'origin_metric_status': {m: _period_status(index, m, origin) for m in DIAGNOSTIC_METRICS}}
        status = row['origin_metric_status']['revenue']
        release = status.get('earliest_preserved_clock')
        row['origin_clock'] = {k: status.get(k) for k in ('earliest_preserved_clock', 'earliest_clock_basis',
                                                          'earliest_vintage', 'earliest_evidence_id')}
        if not targets:
            row['root_causes'].append({'code': 'no_contiguous_target_quarter'})
        if release is None:
            row['root_causes'].append({'code': 'origin_quarter_absent_from_evidence'})
        elif release + timedelta(microseconds=1) >= data.request.research_as_of:
            row['root_causes'].append({'code': 'origin_preserved_only_after_research_cutoff'})
        elif targets and release.date() >= targets[0]['end']:
            # The only preserved versions appeared after the first target quarter ended.
            row['root_causes'].append({'code': 'origin_preserved_only_after_first_target_end',
                                       'detail': {'earliest_clock': release, 'clock_basis': status.get('earliest_clock_basis'),
                                                  'vintage': status.get('earliest_vintage'),
                                                  'first_target_end': targets[0]['end']}})
        if row['root_causes']:
            for c in row['root_causes']:
                causes[c['code']] += 1
            row['eligible'] = False
            rows.append(row)
            continue
        cutoff = release + timedelta(microseconds=1)
        row.update(eligible=True, information_cutoff=cutoff, cutoff_basis=status.get('earliest_clock_basis'))
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
            methods = {'raw_rule': {'errors': _errors(p['values'], _pad(targets)) if p['values'] else None,
                                    'not_applicable_reason': p['reason']}}
            if p['values']:
                guided, used_g, rejected = overlay_guidance(windows, p['values'][:len(targets)], guidance)
                if used_g:
                    methods['guidance_overlay'] = {'errors': _errors([q['value'] for q in guided], _pad(targets)),
                                                   'guidance_evidence_ids': [i for u in used_g for i in u['evidence_ids']]}
                else:
                    methods['guidance_overlay'] = {'errors': None, 'not_applicable_reason':
                                                   'no_preserved_historical_guidance_for_target_quarters' if not rejected
                                                   else 'historical_guidance_rejected_' + rejected[0]['reason']}
            row['candidates'][name] = {'methods': methods, 'training_window': [p['end'] for p in visible[-need:]]}
        for c in row['root_causes']:
            causes[c['code']] += 1
        rows.append(row)
    eligible = [r for r in rows if r.get('eligible')]
    scores = {m: _score(eligible, m) for m in METHODS}
    reason = None
    if not eligible:
        top = Counter(c['code'] for r in rows for c in r['root_causes']).most_common(1)
        reason = top[0][0] if top else 'no_origins'
    return {'method': 'strict_point_in_time_earliest_preserved_version', 'rows': rows, 'scores': scores['raw_rule'],
            'scores_by_method': scores, 'pairwise_by_method': {m: s['pairwise'] for m, s in scores.items()},
            'root_cause_counts': dict(causes), 'eligible_origins': len(eligible), 'origins': len(rows),
            'zero_valid_origin_reason': reason,
            'integrity': {'status': 'pass' if not violations else 'fail', 'violations': violations,
                          'rule': 'every input clock precedes the cutoff; cutoff precedes the first target quarter end'},
            'clock_note': 'Cutoffs use the earliest preserved version of the origin quarter: a source publication date or an '
                          'observed-availability upper bound (conservative, not a publication date). No version is redated.',
            'actuals_basis': 'latest reviewed values for target quarters; target vintages are listed in the filing dataset',
            'limitation': '起點只使用截止前已保留、時鐘已知的版本；觀察到的可得時間只是保守上限，不是原始公布時間。原始申報未經 header 驗證。'}


def evaluate(data, entity_id):
    dataset = filing_dataset(data, entity_id)
    return {'filing_dataset': dataset, 'point_in_time': point_in_time(data, entity_id, dataset),
            'revised_data': revised_data(data, entity_id),
            'claims': 'none: retrospectives are descriptive; no statistical reliability or forecasting superiority is asserted'}
