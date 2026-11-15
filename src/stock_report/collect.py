"""Collect official-source evidence first; apply the research cutoff afterwards.

The order matters. Latest-only endpoints (TWSE/TPEx daily close) never declare a
publication time, so the only honest timestamp is the instant the record was
observed. A research cutoff is therefore supplied *after* collection, and any
historical cutoff that precedes the observation excludes that evidence instead of
quietly re-dating it. A supplied cutoff is never rewritten by this module.
"""
from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Literal
import uuid
from pydantic import AwareDatetime, Field
from .models import Entity, Evidence, Model, Request, ResearchInput, SourceClass
from .mock import VERSIONS
from .providers import HTTPClient
from .quant import calculate
from .licensing import endpoint_in_scope
from .sources import Observation, build_adapter, merge_profile
from .storage import read_json, write_json

CORE_SOURCE_RECORDS_REQUIRED = 2
SEC_CONTACT_ENV = 'STOCK_REPORT_SEC_CONTACT'
DEFAULT_USER_AGENT = 'stock-report-phase0/0.1 (research proof-of-concept)'
SEC_SOURCES = ('sec-xbrl-companyconcept', 'sec-edgar-archives-filing')


class Collection(Model):
    schema_version: Literal['1'] = '1'
    collection_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    source_id: str
    endpoint: str
    capture_mode: Literal['network', 'fixture']
    # Official filing/exchange record or secondary market-data provider. Recorded at capture from
    # the source contract and the licence registry, which must agree.
    source_class: SourceClass = 'official'
    coverage_mode: Literal['latest_only', 'historical_filings']
    requested_ticker: str
    collected_at: AwareDatetime
    entity: Entity
    observations: list[Observation] = Field(min_length=1)
    license: dict
    field_binding: dict[str, str | None] = Field(default_factory=dict)
    transport: list[dict] = Field(default_factory=list)
    profile_overrides: dict = Field(default_factory=dict)
    sec_contact_configured: bool = False
    # A capture from a source whose terms are unconfirmed is a feasibility artifact for
    # verifying the field contract. It is kept locally, never used as research input and
    # never shared; delete it if the terms turn out to forbid retention.
    retention: Literal['research_input_allowed', 'feasibility_only'] = 'feasibility_only'
    notes: list[str] = Field(default_factory=list)


def resolve_sec_contact(explicit=None, environ=None):
    """Explicit operator configuration only. The value is never logged or stored."""
    environ = os.environ if environ is None else environ
    value = (explicit or environ.get(SEC_CONTACT_ENV) or '').strip()
    if not value:
        raise ValueError(f'SEC collection requires an operator-configured contact in --sec-contact or '
                         f'{SEC_CONTACT_ENV}; no login or account data is read or reused')
    if len(value) > 120 or any(c in value for c in '\r\n') or '@' not in value:
        raise ValueError('Configured SEC contact must be a single line containing a contact address; '
                         'the configured value is not echoed')
    return value


def user_agent_for(source_id, user_agent=None, sec_contact=None, environ=None):
    if source_id in SEC_SOURCES:
        return f'{user_agent or DEFAULT_USER_AGENT} {resolve_sec_contact(sec_contact, environ)}'
    return user_agent or DEFAULT_USER_AGENT


def collect(source_id, ticker, *, registry, allow_network=False, fixture=None, client=None, now=None,
            sec_contact=None, environ=None, profile_overrides=None, user_agent=None, retries=1,
            interval=1.0, cache_dir=None, clock=None):
    if source_id == 'sec-edgar-archives-filing':
        from .filing_collect import collect_filings
        if fixture is not None:
            raise ValueError('Reviewed filing collection does not accept fixture captures')
        if not allow_network:
            raise ValueError('Real filing collection requires --allow-network')
        agent = user_agent_for(source_id, user_agent, sec_contact, environ)
        return collect_filings(ticker, registry=registry, allow_network=allow_network,
                               plan=profile_overrides, client=client or HTTPClient(agent, retries=retries,
                               interval=interval), now=now)
    profile = merge_profile(source_id, profile_overrides)
    snapshot = registry.snapshot(source_id)
    licence = registry.get(source_id)
    if not licence.covers_endpoint(profile['endpoint']):
        raise ValueError(f'Endpoint {profile["endpoint"]} is outside the reviewed scope of {source_id} '
                         f'({snapshot["endpoint_scope"]}); a different dataset never inherits reviewed '
                         f'permissions. Register the endpoint before collecting from it.')
    if licence.source_class != profile['source_class']:
        raise ValueError(f'{source_id}: licence registry classifies the source as {licence.source_class!r} but its '
                         f'recorded contract says {profile["source_class"]!r}; resolve the classification first')
    adapter = build_adapter(source_id, profile_overrides)
    specs = adapter.requests(ticker)
    notes, transport, payloads = [], [], {}
    if fixture is not None:
        capture_mode = 'fixture'
        payloads = read_json(fixture)
        if not isinstance(payloads, dict):
            raise ValueError('A fixture capture must be a JSON object keyed by request key')
        unknown = sorted(set(payloads) - {s.key for s in specs})
        if unknown:
            raise ValueError(f'Fixture capture contains keys outside this source contract: {unknown[:8]}')
        transport = [{'key': key, 'status': 'fixture'} for key in sorted(payloads)]
        notes.append('capture_mode=fixture：此採集未接觸來源端點，不得作為真實研究輸入')
    else:
        capture_mode = 'network'
        if not allow_network:
            raise ValueError('Real collection requires --allow-network (or an explicit fixture); '
                             'there is no mock or synthetic fallback')
        if client is None:
            client = HTTPClient(user_agent_for(source_id, user_agent, sec_contact, environ), retries=retries,
                                interval=interval,
                                cache_dir=cache_dir if (cache_dir and snapshot['store_raw_response']) else None)
            if cache_dir and not snapshot['store_raw_response']:
                notes.append('來源未確認可保存原始回應，已停用原始回應快取')
        for spec in specs:
            # Every configured alternative is fetched: only the adapter can tell whether a
            # concept actually carries usable annual facts, so no request is skipped on a guess.
            try:
                data, meta = client.get_json(spec.url)
            except Exception as exc:
                if spec.required:
                    raise
                transport.append({'key': spec.key, 'url': spec.url, 'status': 'unavailable',
                                  'error_type': type(exc).__name__})
                continue
            payloads[spec.key] = data
            row = {'key': spec.key, 'url': spec.url, 'status': 'retrieved',
                   'attempts': meta.get('attempts'), 'cached': meta.get('cached')}
            if snapshot['store_response_hash'] and meta.get('response_sha256'):
                row['response_sha256'] = meta['response_sha256']
            elif meta.get('response_sha256'):
                notes.append(f'{spec.key}：來源未確認可保存回應雜湊，已略過 source_hash')
            transport.append(row)
        if not payloads:
            raise ValueError(f'{source_id}: no payload was retrieved; no fallback data is substituted')
    # Availability can only be asserted once every response has completed: the upper bound is
    # read after transport, never before the request was issued.
    collected_at = now if now is not None else (clock or (lambda: datetime.now(timezone.utc)))()
    parsed = adapter.parse(ticker, payloads, collected_at)
    configured = (sec_contact or (environ if environ is not None else os.environ).get(SEC_CONTACT_ENV) or '').strip()
    if not snapshot['internal_use_allowed']:
        notes.append('授權未確認：本採集僅為欄位契約可行性驗證，不得作為研究輸入，不得散布；'
                     '若條款不允許留存請刪除本地檔案')
    return Collection(collection_id=uuid.uuid4().hex, source_id=source_id, endpoint=profile['endpoint'],
                      capture_mode=capture_mode, source_class=profile['source_class'],
                      coverage_mode=profile['coverage_mode'], requested_ticker=ticker,
                      collected_at=collected_at, entity=parsed.entity, observations=parsed.observations,
                      license=snapshot, field_binding=parsed.field_binding, transport=transport,
                      profile_overrides=profile_overrides or {},
                      sec_contact_configured=bool(source_id in SEC_SOURCES and capture_mode == 'network'
                                                  and configured),
                      retention='research_input_allowed' if snapshot['internal_use_allowed'] else 'feasibility_only',
                      notes=notes + parsed.notes)


def persistence_blockers(collection):
    """Source values may only be written when the registry confirms internal use."""
    if collection.capture_mode == 'fixture':
        return []
    if not collection.license['internal_use_allowed']:
        return [{'code': 'source_values_not_cleared_for_local_persistence', 'source_id': collection.source_id,
                 'detail': f'license_status={collection.license["status"]}；登錄檔未確認內部留存權限，'
                           f'不得寫入任何來源數值，僅能保留檢視用中繼資料'}]
    return []


def write_collection(collection, root):
    """Fail closed before persisting source content."""
    blockers = persistence_blockers(collection)
    if blockers:
        raise ValueError(blockers[0]['detail'])
    directory = Path(root).resolve() / collection.collection_id
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / 'collection.json', collection)
    return directory


def inspection_record(collection):
    """Contract metadata only: field names, counts, status, time and blockers. No source values,
    no source-provided names, no provider record identifiers."""
    return {'artifact': 'inspection_record', 'schema_version': '1',
            'retained': 'contract field names, counts, publication basis, licence status and blockers only',
            'collection_id': collection.collection_id, 'source_id': collection.source_id,
            'endpoint': collection.endpoint, 'capture_mode': collection.capture_mode,
            'source_class': collection.source_class,
            'coverage_mode': collection.coverage_mode, 'requested_ticker': collection.requested_ticker,
            'collected_at': collection.collected_at.isoformat(), 'retention': collection.retention,
            'field_binding': collection.field_binding, 'profile_overrides': collection.profile_overrides,
            'entity_shape': {'exchange': collection.entity.exchange, 'currency': collection.entity.currency,
                             'share_class': collection.entity.share_class, 'name_present': True},
            'observations': [{'kind': o.kind, 'role': o.role, 'unit': o.unit, 'currency': o.currency,
                              'publication_basis': o.publication_basis, 'period_basis': o.period_basis,
                              'period_window_present': o.period_start is not None,
                              'price_adjustment': o.price_adjustment,
                              'source_unit_declared': o.source_unit_raw is not None,
                              'value_present': o.value is not None}
                             for o in collection.observations],
            'transport': [{k: v for k, v in row.items() if k in ('key', 'status', 'attempts', 'cached')}
                          for row in collection.transport],
            'license': {k: collection.license[k] for k in ('source_id', 'registry_version', 'status',
                                                           'human_attested', 'source_reviewed',
                                                           'review_basis', 'endpoint_scope',
                                                           'internal_use_allowed', 'pdf_share_allowed',
                                                           'store_response_hash', 'store_raw_response')},
            'blockers': persistence_blockers(collection), 'notes': collection.notes}


def write_inspection_record(collection, root):
    directory = Path(root).resolve() / collection.collection_id
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / 'inspection-record.json', inspection_record(collection))
    return directory


def persist(collection, root):
    """Write the full collection when permitted, otherwise an inspection-only record.

    Returns (path_of_written_file, artifact_kind)."""
    if persistence_blockers(collection):
        return write_inspection_record(collection, root) / 'inspection-record.json', 'inspection_record'
    return write_collection(collection, root) / 'collection.json', 'collection'


def load_collection(path):
    path = Path(path)
    return Collection.model_validate(read_json(path if path.is_file() else path / 'collection.json'))


def effective_license(collection, registry):
    """Current registry wins, and a permission must hold in both snapshots."""
    current = registry.snapshot(collection.source_id)
    recorded = collection.license
    merged = dict(current)
    for name in ('internal_use_allowed', 'pdf_share_allowed', 'store_response_hash', 'store_raw_response'):
        merged[name] = bool(current.get(name)) and bool(recorded.get(name))
    merged['human_attested'] = bool(current.get('human_attested')) and bool(recorded.get('human_attested'))
    # Captures recorded before `source_reviewed` existed only knew the human attestation.
    merged['source_reviewed'] = (bool(current.get('source_reviewed'))
                                 and bool(recorded.get('source_reviewed', recorded.get('human_attested'))))
    if not merged['source_reviewed']:
        merged['review_basis'] = None
    if current['status'] != recorded.get('status'):
        merged['status'] = 'restricted' if 'restricted' in (current['status'], recorded.get('status')) else 'unknown'
    if current['status'] != recorded.get('status') or current['registry_version'] != recorded.get('registry_version'):
        merged['registry_drift'] = {'recorded': {'status': recorded.get('status'),
                                                 'registry_version': recorded.get('registry_version')},
                                    'current': {'status': current['status'],
                                                'registry_version': current['registry_version']}}
    return merged


def to_evidence(observation, request, license_snapshot, capture_mode, verified_capture, response_hash=None,
                source_class='official'):
    """`verified` means: mechanical source contract checks passed, the registry records a source
    review (a human attestation, or a sufficient assistant review with official evidence made under
    the user's authorization), and the capture itself is accepted as real for this run.

    A fixture capture is authored test content, so its evidence stays `synthetic`: the model
    layer then refuses to carry it inside a `real` dataset at all."""
    verified = (license_snapshot['status'] == 'confirmed' and bool(license_snapshot.get('source_reviewed'))
                and bool(verified_capture))
    return Evidence(
        evidence_id=observation.evidence_id, entity_id=observation.entity_id, claim=observation.claim,
        kind=observation.kind, value=observation.value, unit=observation.unit, currency=observation.currency,
        period=observation.period, period_start=observation.period_start, period_end=observation.period_end,
        period_basis=observation.period_basis, source_url=observation.source_url, publisher=license_snapshot['publisher'],
        # A fixture is authored content whatever it imitates; a real capture keeps the class of its
        # source, so a secondary provider is never presented as an official record.
        source_type=source_class if capture_mode == 'network' else 'synthetic',
        price_adjustment=observation.price_adjustment,
        source_record_id=observation.provider_record_id, metric_key=observation.role,
        source_hash=response_hash if license_snapshot['store_response_hash'] else None,
        event_time=observation.event_time, published_at=observation.published_at,
        publication_basis=observation.publication_basis,
        availability_observed_at=observation.availability_observed_at,
        source_published_raw=observation.source_published_raw, source_period_raw=observation.source_period_raw,
        source_unit_raw=observation.source_unit_raw, retrieved_at=observation.retrieved_at,
        research_as_of=request.research_as_of,
        verification_status='verified' if verified else 'unverified',
        license_status=license_snapshot['status'], attribution=license_snapshot['attribution'],
        shareable_in_pdf=license_snapshot['pdf_share_allowed'],
        internal_use_allowed=license_snapshot['internal_use_allowed'],
        terms_url=license_snapshot['terms_url'], license_checked_at=license_snapshot['checked_at'],
        license_source_id=license_snapshot['source_id'],
        license_registry_version=license_snapshot['registry_version'])


def exclusion_reason(evidence, request):
    if evidence.license_status != 'confirmed' or not evidence.internal_use_allowed:
        return 'license_not_confirmed_for_internal_use'
    if evidence.publication_basis == 'observed_availability' and request.research_as_of < evidence.availability_observed_at:
        return 'historical_as_of_not_supported_by_observed_availability'
    if evidence.published_at > request.research_as_of:
        return 'published_after_research_as_of'
    if evidence.event_time > request.research_as_of:
        return 'event_time_after_research_as_of'
    if evidence.verification_status != 'verified':
        return 'source_not_verified'
    return None


def build_bundle(request, collections, *, registry, allow_fixture_capture=False):
    """Return (research_input_or_None, report). Blockers are reported, never worked around."""
    cutoff = request.research_as_of
    blockers, exclusions, notes, evidence, summary = [], [], [], [], []
    entity = None
    if request.mode != 'stock':
        blockers.append({'code': 'event_mode_real_collection_not_implemented',
                         'detail': '採集流程只支援個股；事件研究須以已查核（人工或合格 AI 查核）的事件卷宗經 event-rank 產生證據包，'
                                   '不以單一公司採集冒充事件研究'})
    if not collections:
        blockers.append({'code': 'no_collection_supplied', 'detail': '沒有任何採集結果，不能產生真實證據包'})
    for collection in collections:
        snapshot = effective_license(collection, registry)
        if not endpoint_in_scope(collection.endpoint, snapshot['endpoint_scope']):
            blockers.append({'code': 'endpoint_outside_license_scope', 'source_id': collection.source_id,
                             'detail': f'{collection.endpoint} 不在已查核範圍 {snapshot["endpoint_scope"]} 之內'})
            continue
        # Every record URL inside a loaded collection is re-bound to the reviewed dataset, so a
        # modified or re-pointed snapshot cannot inherit the reviewed permissions either.
        stray = sorted({o.source_url for o in collection.observations
                        if not endpoint_in_scope(o.source_url, snapshot['endpoint_scope'])})
        if stray:
            blockers.append({'code': 'evidence_url_outside_license_scope', 'source_id': collection.source_id,
                             'detail': f'{stray[:3]} 不在已查核範圍 {snapshot["endpoint_scope"]} 之內'})
            continue
        if registry.get(collection.source_id).source_class != collection.source_class:
            blockers.append({'code': 'source_class_changed_since_capture', 'source_id': collection.source_id,
                             'detail': f'採集時為 {collection.source_class}，登錄檔現為 '
                                       f'{registry.get(collection.source_id).source_class}；不沿用舊分類'})
            continue
        if 'registry_drift' in snapshot:
            notes.append(f'{collection.source_id}：授權登錄檔版本或狀態與採集時不同，採用目前登錄檔的較嚴格結果')
        if collection.capture_mode != 'network' and not allow_fixture_capture:
            blockers.append({'code': 'fixture_capture_not_real_evidence', 'source_id': collection.source_id,
                             'detail': 'capture_mode=fixture 只能用於離線契約測試'})
        if collection.requested_ticker != request.ticker:
            blockers.append({'code': 'collection_ticker_mismatch', 'source_id': collection.source_id,
                             'detail': f'{collection.requested_ticker} != {request.ticker}'})
            continue
        identity = (collection.entity.entity_id, collection.entity.exchange, collection.entity.currency,
                    collection.entity.share_class)
        if entity is None:
            entity = collection.entity
        elif identity != (entity.entity_id, entity.exchange, entity.currency, entity.share_class):
            blockers.append({'code': 'entity_identity_conflict', 'source_id': collection.source_id,
                             'detail': f'{identity} != {(entity.entity_id, entity.exchange, entity.currency, entity.share_class)}'})
            continue
        elif collection.entity.name != entity.name:
            notes.append(f'{collection.source_id}：來源名稱為 {collection.entity.name}，與首個來源不同，已保留首個來源名稱')
        hashes = {row['key']: row.get('response_sha256') for row in collection.transport}
        verified_capture = collection.capture_mode == 'network' or allow_fixture_capture
        kept_here = 0
        for observation in collection.observations:
            record = to_evidence(observation, request, snapshot, collection.capture_mode, verified_capture,
                                 hashes.get(observation.request_key), collection.source_class)
            reason = exclusion_reason(record, request)
            if reason:
                exclusions.append({'evidence_id': record.evidence_id, 'source_id': collection.source_id,
                                   'kind': record.kind, 'metric_key': record.metric_key, 'reason': reason,
                                   'publication_basis': record.publication_basis,
                                   'published_at': record.published_at.isoformat()})
            else:
                evidence.append(record)
                kept_here += 1
        if kept_here == 0:
            blockers.append({'code': 'collection_contributed_no_usable_evidence', 'source_id': collection.source_id,
                             'detail': sorted({row['reason'] for row in exclusions
                                               if row['source_id'] == collection.source_id})})
        summary.append({'collection_id': collection.collection_id, 'source_id': collection.source_id,
                        'capture_mode': collection.capture_mode, 'source_class': collection.source_class,
                        'coverage_mode': collection.coverage_mode,
                        'collected_at': collection.collected_at.isoformat(), 'endpoint': collection.endpoint,
                        'license_status': snapshot['status'], 'human_attested': snapshot['human_attested'],
                        'source_reviewed': snapshot['source_reviewed'], 'review_basis': snapshot.get('review_basis'),
                        'registry_version': snapshot['registry_version'], 'observations': len(collection.observations),
                        'kept': kept_here, 'field_binding': collection.field_binding, 'notes': collection.notes})
    if entity is not None:
        if request.exchange and entity.exchange != request.exchange:
            blockers.append({'code': 'request_exchange_mismatch',
                             'detail': f'{entity.exchange} != {request.exchange}'})
        if entity.entity_id != request.ticker:
            blockers.append({'code': 'entity_ticker_mismatch', 'detail': f'{entity.entity_id} != {request.ticker}'})
    seen = Counter(e.evidence_id for e in evidence)
    duplicates = sorted(eid for eid, count in seen.items() if count > 1)
    if duplicates:
        blockers.append({'code': 'duplicate_evidence_id', 'detail': duplicates[:8]})
    core = {(e.publisher, e.source_record_id) for e in evidence if e.kind in ('financial', 'price', 'event')}
    if not any(e.kind == 'identity' for e in evidence):
        blockers.append({'code': 'identity_evidence_unavailable',
                         'detail': '沒有通過授權與時點檢查的身份證據，不產生真實研究輸入'})
    coverage_warnings = []
    if len(core) < CORE_SOURCE_RECORDS_REQUIRED:
        # Not a blocker: the horizon Gate alone decides whether a direction may be published.
        # A licensed, verified identity still deserves an explicit insufficient-evidence report.
        coverage_warnings.append({'code': 'core_evidence_below_direction_threshold',
                                  'detail': f'可用核心來源紀錄 {len(core)} < {CORE_SOURCE_RECORDS_REQUIRED}；'
                                            f'Gate 會把各期限降級為 insufficient，不會發布方向'})
    # A fixture capture is authored test content and is labelled as such for the whole pipeline:
    # it can never be promoted into a real-data bundle, with or without the opt-in flag.
    dataset_kind = 'real' if all(c.capture_mode == 'network' for c in collections) and collections else 'mock'
    report = {'status': 'blocked' if blockers else 'ready', 'dataset_kind': dataset_kind,
              'request': {'mode': request.mode, 'ticker': request.ticker, 'exchange': request.exchange,
                          'research_as_of': cutoff.isoformat()},
              'research_as_of_handling': '研究截止時點取自 Request 檔，採集流程不會改寫；不支援的歷史時點證據一律排除',
              'collections': summary, 'kept_evidence': sorted(e.evidence_id for e in evidence),
              'core_source_records': len(core), 'direction_threshold': CORE_SOURCE_RECORDS_REQUIRED,
              'coverage_warnings': coverage_warnings, 'exclusions': exclusions, 'blockers': blockers,
              'notes': notes}
    if request.research_as_of != cutoff:
        raise ValueError('The supplied research cutoff must never be modified')
    if blockers:
        return None, report
    data = ResearchInput(dataset_kind=dataset_kind, request=request, entities=[entity], evidence=evidence,
                         metrics=calculate(evidence, cutoff), relationships=[], event_evidence_ids=[],
                         versions={**VERSIONS, 'dataset': 'real-collected-1' if dataset_kind == 'real'
                                   else 'fixture-capture-1'})
    report['metrics'] = [{'name': m.name, 'value': m.value, 'missing_reason': m.missing_reason} for m in data.metrics]
    return data, report


def request_template(mode, ticker, exchange, collections, research_as_of=None):
    """Cutoff after collection: a latest-only run cannot be dated before its observations."""
    latest = max(c.collected_at for c in collections)
    chosen = research_as_of or latest
    if chosen < latest and any(c.coverage_mode == 'latest_only' for c in collections):
        raise ValueError('A research cutoff earlier than the latest-only collection cannot be supported; '
                         'collect first and date the request at or after the collection time, or supply '
                         'source-declared historical evidence')
    return Request(mode=mode, ticker=ticker, exchange=exchange, research_as_of=chosen)
