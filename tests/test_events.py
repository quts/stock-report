"""Event verification and candidate ranking: evidence-bound, deterministic, bounded universe."""
from datetime import timedelta
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from pypdf import PdfReader
from stock_report.cli import main
from stock_report.events import (EventDossier, EventSource, Link, load_universe, rank_dossier,
                                 synthetic_dossier)
from stock_report.licensing import load_registry
from stock_report.models import Request
from stock_report.pipeline import prepare
from stock_report.storage import digest, read_json, write_json

EXAMPLE = Path('examples/events/dossier-synthetic.json')


@pytest.fixture
def universe():
    return load_universe()


@pytest.fixture
def dossier():
    return synthetic_dossier(Request.model_validate(read_json('examples/event.json')))


def edit(dossier, evidence_id, **changes):
    dossier.evidence = [e.model_copy(update=changes) if e.evidence_id == evidence_id else e for e in dossier.evidence]
    return dossier


def revalidate(dossier):
    return EventDossier.model_validate(dossier.model_dump(mode='json'))


def as_real(dossier, reviewed_by='human'):
    """Test-only relabelling of authored records, to exercise the real-dossier checks."""
    payload = dossier.model_dump(mode='json')
    payload['dossier_kind'] = 'real'
    payload['review'].update(reviewed_by=reviewed_by, reviewer='Test Reviewer (unit test, not a real attestation)')
    for e in payload['evidence']:
        e.update(source_type='official', publication_basis='source_declared_timestamp')
    return EventDossier.model_validate(payload)


def by_entity(report):
    return {row['entity_id']: row for row in report['ranking']}


def reasons(report):
    return {row['entity_id']: row['reason'] for row in report['excluded_candidates']}


def codes(report):
    return {b['code'] for b in report['blockers']}


def test_universe_matches_the_explicit_identity_configurations(universe):
    sec = {e['ticker']: e for e in read_json('config/sec-entities.json')['entities']}
    tw = {e['ticker']: e for e in read_json('config/tw-instruments.json')['instruments']}
    assert {e.entity_id for e in universe.entities} == set(sec) | set(tw)
    for e in universe.entities:
        row = sec.get(e.entity_id) or tw[e.entity_id]
        assert (row['exchange'], row['share_class']) == (e.exchange, e.share_class)


@pytest.mark.parametrize('source_id', [None, 'unregistered-provider'])
def test_missing_or_unknown_id_cannot_open_a_registered_closed_endpoint(dossier, universe, source_id):
    real = as_real(dossier)
    edit(real, 'EVENT-announcement',
         source_url='https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
         license_source_id=source_id)
    data, report = rank_dossier(real, universe, load_registry())
    assert data is None
    assert report['event']['verification']['excluded'][0]['reason'] in (
        'registry_keeps_source_closed', 'source_not_in_registry')


def test_stale_event_source_does_not_satisfy_news_window(dossier, universe):
    edit(dossier, 'EVENT-announcement',
         published_at=dossier.request.research_as_of - timedelta(days=dossier.request.lookback_days + 1))
    data, report = rank_dossier(dossier, universe)
    assert data is None
    assert report['event']['verification']['excluded'][0]['reason'] == 'event_source_outside_news_window'


def test_committed_example_is_the_generated_synthetic_dossier(dossier):
    assert read_json(EXAMPLE) == dossier.model_dump(mode='json')


def test_ranking_follows_evidence_strength_and_bounds_the_universe(dossier, universe):
    data, report = rank_dossier(dossier, universe)
    assert report['status'] == 'ready' and report['data_label'] == 'DEMO / SYNTHETIC DATA'
    assert [(r['rank'], r['entity_id'], r['relationship']) for r in report['ranking']] == [
        (1, '2330.TW', 'direct'), (2, 'NVDA', 'direct'), (3, 'AMD', 'indirect'), (4, '6488.TWO', 'hypothetical')]
    assert report['ranking'][0]['rank_key']['official_origins'] == 2
    assert reasons(report) == {'INTC': 'outside_supported_universe'}
    assert 'INTC' not in report['universe']['coverage']
    assert report['event']['verification']['level'] == 'official'
    assert data.dataset_kind == 'mock' and [e.entity_id for e in data.entities] == report['selected']
    assert {r.entity_id: r.relationship for r in data.relationships} == {
        '2330.TW': 'direct', 'NVDA': 'direct', 'AMD': 'indirect', '6488.TWO': 'hypothetical'}
    assert not any(e.entity_id == 'INTC' for e in data.evidence)
    assert report['bundle_sha256'] == digest(data)


def test_declared_label_never_overrides_the_derived_class(dossier, universe):
    _, report = rank_dossier(dossier, universe)
    amd = by_entity(report)['AMD']
    assert amd['declared_relationship'] == 'direct' and amd['relationship'] == 'indirect'
    assert 'declared_relationship_overclaimed' in amd['flags']
    assert any('AMD' in w for w in report['warnings'])


def test_ranking_is_independent_of_input_order_and_reproducible(dossier, universe):
    shuffled = dossier.model_copy(deep=True)
    shuffled.candidates.reverse()
    shuffled.evidence.reverse()
    for candidate in shuffled.candidates:
        candidate.links.reverse()
    first, second = rank_dossier(dossier, universe), rank_dossier(shuffled, universe)
    again = rank_dossier(dossier, universe)
    assert json.dumps(first[1], sort_keys=True) == json.dumps(again[1], sort_keys=True)
    strip = lambda report: {k: v for k, v in report.items() if k != 'dossier_sha256'}
    assert strip(first[1]) == strip(second[1])
    assert digest(first[0]) == digest(second[0])


def test_report_carries_ids_only_and_never_source_text(dossier, universe):
    _, report = rank_dossier(dossier, universe)
    text = json.dumps(report, ensure_ascii=False)
    assert not any(e.claim in text for e in dossier.evidence)


def test_company_limit_selects_the_top_of_the_order(dossier, universe):
    dossier.request = dossier.request.model_copy(update={'max_companies': 2})
    data, report = rank_dossier(dossier, universe)
    assert report['selected'] == ['2330.TW', 'NVDA']
    assert [e.entity_id for e in data.entities] == ['2330.TW', 'NVDA']
    assert [r['selected'] for r in report['ranking']] == [True, True, False, False]
    assert {e.entity_id for e in data.evidence} == {'2330.TW', 'NVDA', 'EVENT'}


def test_direct_claim_backed_only_by_secondary_source_is_not_direct(dossier, universe):
    nvda = next(c for c in dossier.candidates if c.entity.entity_id == 'NVDA')
    nvda.links = [l.model_copy(update={'tier': 'secondary'}) for l in nvda.links]
    _, report = rank_dossier(dossier, universe)
    row = by_entity(report)['NVDA']
    assert row['relationship'] == 'indirect' and 'direct_claim_not_officially_confirmed' in row['flags']
    # AMD and NVDA are now both indirect with one origin: the entity ID breaks the tie.
    assert [r['entity_id'] for r in report['ranking']] == ['2330.TW', 'AMD', 'NVDA', '6488.TWO']


def test_declared_official_tier_needs_an_official_record_in_a_real_dossier(dossier, universe):
    real = as_real(dossier)
    payload = real.model_dump(mode='json')
    for e in payload['evidence']:
        if e['evidence_id'] == 'NVDA-relationship':
            e['source_type'] = 'secondary'
    _, report = rank_dossier(EventDossier.model_validate(payload), universe, load_registry())
    assert by_entity(report)['NVDA']['relationship'] == 'indirect'
    assert by_entity(report)['NVDA']['links'][0]['tier'] == 'secondary'


def test_copies_of_one_source_do_not_count_as_independent_origins(dossier, universe):
    tsmc = next(c for c in dossier.candidates if c.entity.entity_id == '2330.TW')
    first = next(e for e in dossier.evidence if e.evidence_id == '2330.TW-relationship')
    edit(dossier, '2330.TW-relationship-filing', source_url=first.source_url)
    _, report = rank_dossier(dossier, universe)
    assert len(tsmc.links) == 2
    assert by_entity(report)['2330.TW']['rank_key']['official_origins'] == 1
    # With equal strength the tie is broken by entity ID, not by the number of copies.
    assert [r['entity_id'] for r in report['ranking']][:2] == ['2330.TW', 'NVDA']


@pytest.mark.parametrize('changes, reason', [
    ({'verification_status': 'unverified'}, 'source_not_verified'),
    ({'verification_status': 'conflicting'}, 'source_not_verified'),
    ({'license_status': 'unknown'}, 'license_not_confirmed_for_internal_use'),
    ({'license_status': 'restricted'}, 'license_not_confirmed_for_internal_use'),
    ({'internal_use_allowed': False}, 'license_not_confirmed_for_internal_use'),
    ({'entity_id': 'NVDA'}, 'evidence_belongs_to_another_entity'),
    ({'kind': 'event'}, 'evidence_kind_is_not_relationship'),
])
def test_unusable_link_evidence_removes_the_candidate(dossier, universe, changes, reason):
    edit(dossier, 'AMD-relationship', **changes)
    data, report = rank_dossier(dossier, universe)
    assert reasons(report)['AMD'] == 'relationship_evidence_unusable'
    assert 'AMD' not in report['selected'] and 'AMD' not in {e.entity_id for e in data.entities}
    excluded = next(r for r in report['excluded_candidates'] if r['entity_id'] == 'AMD')
    assert excluded['links'][0]['reason'] == reason
    assert report['universe']['coverage']['AMD'] == 'relationship_evidence_unusable'


def test_link_published_after_the_cutoff_is_not_used(dossier, universe):
    cutoff = dossier.request.research_as_of
    edit(dossier, 'AMD-relationship', published_at=cutoff + timedelta(seconds=1))
    _, report = rank_dossier(dossier, universe)
    assert next(r for r in report['excluded_candidates'] if r['entity_id'] == 'AMD')['links'][0]['reason'] \
        == 'published_after_research_as_of'
    edit(dossier, 'NVDA-relationship', research_as_of=cutoff - timedelta(days=1))
    _, report = rank_dossier(dossier, universe)
    assert reasons(report)['NVDA'] == 'relationship_evidence_unusable'


def test_unknown_link_evidence_id_is_reported_not_guessed(dossier, universe):
    amd = next(c for c in dossier.candidates if c.entity.entity_id == 'AMD')
    amd.links = [Link(evidence_id='does-not-exist', origin_id='x', tier='official', basis='named_in_event_source')]
    _, report = rank_dossier(dossier, universe)
    assert next(r for r in report['excluded_candidates'] if r['entity_id'] == 'AMD')['links'][0]['reason'] \
        == 'unknown_evidence_id'


def test_candidate_without_identity_or_links_is_not_ranked(dossier, universe):
    edit(dossier, 'NVDA-identity', verification_status='unverified')
    next(c for c in dossier.candidates if c.entity.entity_id == 'AMD').links = []
    _, report = rank_dossier(dossier, universe)
    assert reasons(report)['NVDA'] == 'identity_evidence_unavailable'
    assert reasons(report)['AMD'] == 'no_relationship_evidence'
    assert report['selected'] == ['2330.TW', '6488.TWO']


def test_identity_conflict_with_the_universe_is_excluded(dossier, universe):
    payload = dossier.model_dump(mode='json')
    next(c for c in payload['candidates'] if c['entity']['entity_id'] == 'NVDA')['entity']['exchange'] = 'NYSE'
    _, report = rank_dossier(EventDossier.model_validate(payload), universe)
    assert reasons(report)['NVDA'] == 'identity_conflicts_with_universe'


def test_universe_members_absent_from_the_dossier_are_reported_not_invented(dossier, universe):
    dossier.candidates = [c for c in dossier.candidates if c.entity.entity_id != '6488.TWO']
    _, report = rank_dossier(dossier, universe)
    assert report['universe']['coverage']['6488.TWO'] == 'not_in_dossier'
    assert '6488.TWO' not in by_entity(report)


@pytest.mark.parametrize('mutate, code', [
    (lambda d: setattr(d.event, 'status', 'rumored'), 'event_status_unconfirmed'),
    (lambda d: edit(d, 'EVENT-announcement', verification_status='unverified'), 'event_not_verified'),
    (lambda d: edit(d, 'EVENT-announcement', license_status='unknown'), 'event_not_verified'),
    (lambda d: edit(d, 'EVENT-announcement', kind='relationship'), 'event_not_verified'),
    (lambda d: edit(d, 'EVENT-announcement', published_at=d.request.research_as_of + timedelta(days=1)),
     'event_not_verified'),
    (lambda d: setattr(d, 'event_sources', []), 'event_not_verified'),
    (lambda d: setattr(d.event, 'event_time', d.request.research_as_of + timedelta(days=1)),
     'event_time_after_research_as_of'),
])
def test_unestablished_event_blocks_ranking_output(dossier, universe, mutate, code):
    mutate(dossier)
    data, report = rank_dossier(dossier, universe)
    assert data is None and report['status'] == 'blocked' and code in codes(report)
    assert 'bundle_sha256' not in report


def second_event_source(dossier, **changes):
    first = next(e for e in dossier.evidence if e.evidence_id == 'EVENT-announcement')
    update = {'evidence_id': 'EVENT-report-2', 'source_record_id': 'report-2',
              'source_url': 'https://example.invalid/synthetic/EVENT/report-2', 'publisher': 'Synthetic wire B'}
    dossier.evidence.append(first.model_copy(update={**update, **changes}))
    dossier.review.evidence_ids.append('EVENT-report-2')


def test_secondary_reports_need_two_independent_origins(dossier, universe):
    dossier.event_sources = [EventSource(evidence_id='EVENT-announcement', origin_id='a', tier='secondary')]
    assert 'event_not_verified' in codes(rank_dossier(dossier, universe)[1])
    second_event_source(dossier)
    dossier.event_sources.append(EventSource(evidence_id='EVENT-report-2', origin_id='b', tier='secondary'))
    data, report = rank_dossier(dossier, universe)
    assert report['event']['verification']['level'] == 'secondary_corroborated'
    assert data.event_evidence_ids == ['EVENT-announcement', 'EVENT-report-2']


@pytest.mark.parametrize('origin, changes', [
    ('a', {}),                                                              # syndicated copy of one release
    ('b', {'publisher': 'Synthetic fixture'}),                              # same publisher twice
    ('b', {'source_url': 'https://example.invalid/synthetic/EVENT/announcement'}),  # same URL
])
def test_same_origin_secondary_reports_do_not_corroborate(dossier, universe, origin, changes):
    second_event_source(dossier, **changes)
    dossier.event_sources = [EventSource(evidence_id='EVENT-announcement', origin_id='a', tier='secondary'),
                             EventSource(evidence_id='EVENT-report-2', origin_id=origin, tier='secondary')]
    data, report = rank_dossier(dossier, universe)
    assert data is None and 'event_not_verified' in codes(report)


def test_proposed_event_is_flagged_as_not_in_effect(dossier, universe):
    dossier.event.status = 'proposed'
    _, report = rank_dossier(dossier, universe)
    assert report['status'] == 'ready' and any('proposed' in w for w in report['warnings'])


def test_no_rankable_candidate_is_a_blocker(dossier, universe):
    dossier.candidates = [c for c in dossier.candidates if c.entity.entity_id == 'INTC']
    data, report = rank_dossier(dossier, universe)
    assert data is None and codes(report) == {'no_rankable_candidate'}


def test_dossier_kinds_never_mix(dossier):
    payload = dossier.model_dump(mode='json')
    payload['dossier_kind'] = 'real'
    with pytest.raises(ValidationError, match='Synthetic evidence cannot appear in a real dossier'):
        EventDossier.model_validate(payload)
    payload = dossier.model_dump(mode='json')
    payload['evidence'][0].update(source_type='official', publication_basis='source_declared_timestamp')
    with pytest.raises(ValidationError, match='only contain synthetic evidence'):
        EventDossier.model_validate(payload)
    payload = dossier.model_dump(mode='json')
    payload['review']['reviewed_by'] = 'human'
    with pytest.raises(ValidationError, match='synthetic fixture review'):
        EventDossier.model_validate(payload)
    payload = dossier.model_dump(mode='json')
    payload['request'] = read_json('examples/stock-nvda.json')
    with pytest.raises(ValidationError, match='event-mode request'):
        EventDossier.model_validate(payload)


def test_real_dossier_requires_a_human_review(dossier, universe):
    data, report = rank_dossier(as_real(dossier, 'assistant'), universe, load_registry())
    assert data is None and 'dossier_requires_human_review' in codes(report)
    assert report['event']['verification']['excluded'] == [
        {'evidence_id': 'EVENT-announcement', 'reason': 'not_covered_by_human_review'}]


def test_human_reviewed_real_dossier_yields_a_real_bundle(dossier, universe, tmp_path):
    real = as_real(dossier)
    data, report = rank_dossier(real, universe, load_registry())
    assert report['status'] == 'ready' and report['data_label'].startswith('REAL')
    assert data.dataset_kind == 'real' and data.versions['dataset'] == 'real-event-dossier-1'
    prepared = prepare(real.request, tmp_path, data.model_dump(mode='json'))
    assert prepared['warnings'] == ['Real evidence import requires human source/license verification']
    with pytest.raises(ValueError, match='synthetic event provider only accepts'):
        prepare(real.request, tmp_path, data.model_dump(mode='json'), synthetic_source=True)


def test_evidence_outside_the_human_review_list_is_excluded(dossier, universe):
    real = as_real(dossier)
    real.review.evidence_ids.remove('AMD-relationship')
    real.review.evidence_ids.remove('NVDA-net_income')
    data, report = rank_dossier(real, universe, load_registry())
    assert reasons(report)['AMD'] == 'relationship_evidence_unusable'
    assert {'evidence_id': 'NVDA-net_income', 'reason': 'not_covered_by_human_review'} in report['bundle_evidence_exclusions']
    assert 'NVDA-net_income' not in {e.evidence_id for e in data.evidence}


def test_dossier_cannot_open_a_source_the_registry_keeps_closed(dossier, universe):
    real = as_real(dossier)
    edit(real, '2330.TW-close', license_source_id='twse-openapi-stock-day-all',
         source_url='https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL')
    edit(real, '2330.TW-identity', license_source_id='twse-openapi-stock-day-all',
         source_url='https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL')
    data, report = rank_dossier(real, universe, load_registry())
    # The current registry keeps every source unknown: the identity is unusable, so 2330 drops out.
    assert reasons(report)['2330.TW'] == 'identity_evidence_unavailable'
    assert '2330.TW' not in {e.entity_id for e in data.evidence}
    attested = load_registry('tests/fixtures/licenses-attested.json')
    data, report = rank_dossier(real, universe, attested)
    assert report['ranking'][0]['entity_id'] == '2330.TW'
    edit(real, '2330.TW-identity', source_url='https://openapi.twse.com.tw/v1/other/dataset')
    assert reasons(rank_dossier(real, universe, attested)[1])['2330.TW'] == 'identity_evidence_unavailable'


def run_cli(capsys, *argv):
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_cli_ranks_then_runs_the_labelled_synthetic_event_report(tmp_path, capsys):
    code, ranked = run_cli(capsys, 'event-rank', '--dossier', str(EXAMPLE), '--output', str(tmp_path / 'events'))
    assert code == 0 and ranked['status'] == 'ready' and ranked['next_provider'] == 'synthetic-event'
    assert [r['entity_id'] for r in ranked['ranking']] == ['2330.TW', 'NVDA', 'AMD', '6488.TWO']
    assert ranked['excluded'] == [{'entity_id': 'INTC', 'reason': 'outside_supported_universe'}]
    first = Path(ranked['ranking_path']).read_bytes()
    assert run_cli(capsys, 'event-rank', '--dossier', str(EXAMPLE), '--output', str(tmp_path / 'events'))[0] == 0
    assert Path(ranked['ranking_path']).read_bytes() == first
    # A synthetic bundle can never enter through the reviewed-real import path.
    code, refused = run_cli(capsys, 'prepare', '--request', ranked['request_path'], '--provider', 'import',
                            '--evidence-bundle', ranked['bundle_path'], '--output', str(tmp_path))
    assert code == 1 and 'reserved for explicitly reviewed real evidence' in refused['message']
    code, prepared = run_cli(capsys, 'prepare', '--request', ranked['request_path'], '--provider', 'synthetic-event',
                             '--evidence-bundle', ranked['bundle_path'], '--output', str(tmp_path))
    assert code == 0 and prepared['warnings'] == ['DEMO / SYNTHETIC DATA']
    run = Path(prepared['research_input_path']).parent
    assert read_json(run / 'research_input.json')['versions']['dataset'] == 'synthetic-event-dossier-1'
    assert run_cli(capsys, 'analyze', '--run', str(run), '--synthetic')[0] == 0
    code, final = run_cli(capsys, 'finalize', '--run', str(run), '--proposal', str(run / 'ai_proposal.json'))
    assert code == 0 and final['warnings'][0] == 'DEMO / SYNTHETIC DATA'
    decision = read_json(run / 'decision.json')
    outlooks = {(p['entity_id'], p['horizon']): p['outlook'] for p in decision['outlooks']}
    assert outlooks[('6488.TWO', '3-6m')] == 'insufficient' and outlooks[('2330.TW', '3-6m')] == 'bullish'
    assert 'HYPOTHETICAL' in {f['rule_id'] for f in decision['findings'] if f['entity_id'] == '6488.TWO'}
    pages = PdfReader(final['report_path']).pages
    assert 1 <= len(pages) <= 2 and 'DEMO' in pages[0].extract_text()
    assert read_json(run / 'manifest.json')['dataset_kind'] == 'mock'


def test_cli_blocked_dossier_exits_nonzero_and_leaves_no_bundle(tmp_path, capsys, dossier):
    out = tmp_path / 'events'
    path = tmp_path / 'dossier.json'
    write_json(path, dossier)
    code, ready = run_cli(capsys, 'event-rank', '--dossier', str(path), '--output', str(out))
    assert code == 0 and Path(ready['bundle_path']).is_file()
    dossier.event.status = 'rumored'
    write_json(path, dossier)
    code, blocked = run_cli(capsys, 'event-rank', '--dossier', str(path), '--output', str(out))
    assert code == 1 and blocked['status'] == 'blocked' and 'bundle_path' not in blocked
    directory = Path(blocked['ranking_path']).parent
    assert sorted(p.name for p in directory.iterdir()) == ['event-ranking.json']


def test_cli_real_dossier_without_human_review_is_blocked(tmp_path, capsys, dossier):
    path = tmp_path / 'dossier.json'
    write_json(path, as_real(dossier, 'assistant'))
    code, blocked = run_cli(capsys, 'event-rank', '--dossier', str(path), '--output', str(tmp_path / 'events'))
    assert code == 1 and 'dossier_requires_human_review' in {b['code'] for b in blocked['blockers']}


def test_synthetic_event_provider_rejects_misuse(tmp_path, capsys, dossier, universe):
    code, out = run_cli(capsys, 'prepare', '--request', 'examples/event.json', '--provider', 'synthetic-event')
    assert code == 1 and out['status'] == 'failed'
    data, _ = rank_dossier(dossier, universe)
    other = Request.model_validate({**read_json('examples/event.json'), 'event_query': '另一個事件'})
    with pytest.raises(ValueError, match='does not match'):
        prepare(other, tmp_path, data.model_dump(mode='json'), synthetic_source=True)
