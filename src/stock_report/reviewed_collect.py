"""Bounded official filing discovery and reviewed publisher facts; no full-text retention."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
import uuid
from xml.etree import ElementTree
from .models import Entity, FilingDiscovery
from .sources import Observation
from .storage import read_json
from .filing_collect import FilingPlan, Text, normalized

DELL_SCHEDULE = ('https://investors.delltechnologies.com/news-events/press-releases/detail/1661/'
                 'dell-technologies-to-hold-conference-call-nov-24-to-discuss-third-quarter-fiscal-2027-financial-results')
REVIEWED_PROFILES = {
    'sec-submissions-history': {
        'adapter': 'sec_submissions', 'source_class': 'official',
        'endpoint': 'https://data.sec.gov/submissions/CIK{cik}.json',
        'identity_binding': {'cik': ['cik'], 'entity_name': ['name'], 'filings': ['filings.recent']},
    },
    'dell-ir-next-earnings-1661': {
        'adapter': 'reviewed_html', 'source_class': 'official', 'endpoint': DELL_SCHEDULE,
        'identity_binding': {'issuer': ['NYSE: DELL'], 'publication': ['Released']},
        'tickers': ['DELL'],
    },
    'lemonde-economy-rss': {
        'adapter': 'reviewed_rss', 'source_class': 'secondary',
        'endpoint': 'https://www.lemonde.fr/en/economy/rss_full.xml',
        'identity_binding': {'publisher': ['channel.title'], 'publication': ['item.pubDate']},
    },
}


def issuer(ticker):
    rows = [r for r in read_json('config/sec-entities.json')['entities'] if r['ticker'] == ticker]
    if len(rows) != 1:
        raise ValueError('Issuer must be uniquely configured')
    row = rows[0]
    return row, Entity(entity_id=ticker, name=row['expected_entity_name_contains'], exchange=row['exchange'],
                       currency=row['currency'], share_class=row['share_class'])


def parse_submissions(payload, row, as_of, observed):
    """Select by actual public availability, excluding future filings and amendments as replacements."""
    if int(payload.get('cik', -1)) != int(row['cik']) or row['expected_entity_name_contains'].casefold() not in str(payload.get('name', '')).casefold():
        raise ValueError('Submissions response belongs to another issuer')
    recent = payload.get('filings', {}).get('recent', {})
    fields = ('form', 'filingDate', 'reportDate', 'accessionNumber', 'primaryDocument')
    if not all(isinstance(recent.get(k), list) for k in fields):
        raise ValueError('SEC submissions contract changed')
    size = len(recent['form'])
    if size > 20000 or any(len(recent[k]) != size for k in fields):
        raise ValueError('SEC filing columns have inconsistent lengths')
    candidates = []
    for i, form in enumerate(recent['form']):
        if form not in ('10-Q', '10-K', '10-Q/A', '10-K/A'):
            continue
        accepted = recent.get('acceptanceDateTime', [])
        pub = (datetime.fromisoformat(accepted[i].replace('Z', '+00:00'))
               if len(accepted) == size and accepted[i] else
               datetime.fromisoformat(recent['filingDate'][i] + 'T23:59:59.999999-05:00'))
        if pub.tzinfo is None:
            raise ValueError('SEC acceptance time must have an explicit timezone')
        accession, document = recent['accessionNumber'][i], recent['primaryDocument'][i]
        if not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}', document):
            raise ValueError('Unexpected SEC archive path')
        if pub <= as_of:
            candidates.append((pub, recent['reportDate'][i], form, accession, document))
    if not candidates:
        raise ValueError('No eligible financial filing in the available SEC index; historical coverage not assumed')
    # The latest reporting period wins; an amendment filed later for an older period never displaces
    # a newer original 10-Q/10-K, and for the same period the original is preferred over its amendment.
    pub, period, form, accession, document = max(candidates, key=lambda c: (c[1], not c[2].endswith('/A'), c[0]))
    return FilingDiscovery(entity_id=row['ticker'], cik=row['cik'], as_of=as_of, discovered_at=observed,
        form=form, accession=accession, period_end=period, published_at=pub,
        document_url=f'https://www.sec.gov/Archives/edgar/data/{int(row["cik"])}/{accession.replace("-", "")}/{document}',
        discovery_evidence_id=f'sec-{row["ticker"]}-latest-filing')


def collect_reviewed(source_id, ticker, *, registry, plan, client, now=None):
    from .collect import Collection
    profile, (row, entity) = REVIEWED_PROFILES[source_id], issuer(ticker)
    snapshot = registry.snapshot(source_id)
    # Unconfirmed publishers stay closed: nothing is fetched until a sufficient review opened them.
    if not snapshot['source_reviewed'] or not snapshot['internal_use_allowed']:
        raise ValueError(f'{source_id}: source review or internal-use permission is not confirmed; collection stays closed')
    if not registry.get(source_id).covers_endpoint(profile['endpoint']):
        raise ValueError('Publisher endpoint is outside registered scope')
    if snapshot['source_class'] != profile['source_class']:
        raise ValueError('Source class differs from reviewed contract')
    if profile.get('tickers') and ticker not in profile['tickers']:
        raise ValueError('Issuer is outside this publisher contract')
    endpoint = profile['endpoint'].format(cik=row['cik'])
    discovery, observations, transport, notes = None, [], [], []
    if profile['adapter'] == 'sec_submissions':
        if not isinstance(plan, dict) or set(plan) != {'as_of'}:
            raise ValueError('Latest-filing discovery requires only an explicit as_of')
        as_of = datetime.fromisoformat(plan['as_of'].replace('Z', '+00:00'))
        if as_of.tzinfo is None:
            raise ValueError('Discovery cutoff must be timezone-aware')
        payload, meta = client.get_json(endpoint)
        observed = now or datetime.now(timezone.utc)
        if as_of > observed:
            raise ValueError('A future cutoff cannot be reviewed')
        discovery = parse_submissions(payload, row, as_of, observed)
        observations.append(Observation(evidence_id=discovery.discovery_evidence_id, entity_id=ticker,
            kind='identity', claim=f'{entity.name}: latest eligible {discovery.form}, accession {discovery.accession}.',
            provider_record_id=discovery.accession, request_key=endpoint, source_url=endpoint,
            publication_basis='source_declared_timestamp', published_at=discovery.published_at,
            event_time=discovery.published_at, retrieved_at=observed))
        transport.append({'key': endpoint, 'status': 'retrieved', 'attempts': meta.get('attempts')})
    elif profile['adapter'] == 'reviewed_html':
        facts = FilingPlan.model_validate(plan)
        if any(f.url != endpoint for f in facts.facts):
            raise ValueError('Reviewed article plan cannot request another endpoint')
        raw, meta = client.fetch(endpoint, 'text/html')
        parser = Text(); parser.feed(raw.decode('utf-8'))
        text, observed = normalized(' '.join(parser.parts)), now or datetime.now(timezone.utc)
        if not any(normalized(x) in text for x in profile['identity_binding']['issuer']):
            raise ValueError('Article issuer identity does not match')
        for fact in facts.facts:
            if any(len(normalized(q)) < 12 or normalized(q) not in text for q in fact.excerpts):
                raise ValueError('Reviewed publisher excerpt absent from live article')
            pub = datetime.fromisoformat(fact.publication_date + 'T23:59:59.999999-05:00')
            event = datetime.fromisoformat(fact.event_date + 'T00:00:00-05:00')
            if pub > observed or event > observed:
                raise ValueError('Future publication cannot be established')
            observations.append(Observation(evidence_id=fact.evidence_id, entity_id=ticker, kind='event',
                claim=fact.claim, provider_record_id=fact.evidence_id, request_key=source_id, source_url=endpoint,
                publication_basis='source_declared_date', published_at=pub, event_time=event, retrieved_at=observed,
                source_published_raw=fact.publication_date, statement_kind=fact.statement_kind,
                target_period=fact.target_period, institution=fact.institution))
        transport.append({'key': endpoint, 'status': 'retrieved', 'attempts': meta.get('attempts')})
    else:
        if not isinstance(plan, dict) or set(plan) != {'keywords'} or not 1 <= len(plan['keywords']) <= 12:
            raise ValueError('RSS collection requires a bounded keyword list')
        if any(not isinstance(k, str) or not 2 <= len(k) <= 80 for k in plan['keywords']):
            raise ValueError('Invalid RSS keyword')
        raw, meta = client.fetch(endpoint, 'application/rss+xml, application/xml, text/xml')
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('DTD and external entities are forbidden')
        root = ElementTree.fromstring(raw)
        channel = root.find('channel')
        if channel is None or 'monde' not in (channel.findtext('title') or '').casefold():
            raise ValueError('RSS publisher identity changed')
        observed = now or datetime.now(timezone.utc)
        items = channel.findall('item')
        if len(items) > 200:
            raise ValueError('RSS item bound exceeded')
        for i, item in enumerate(items):
            title = item.findtext('title') or ''
            parser = Text(); parser.feed(item.findtext('description') or '')
            summary = normalized(' '.join(parser.parts))
            if not any(k.casefold() in (title + ' ' + summary).casefold() for k in plan['keywords']):
                continue
            stamp = parsedate_to_datetime(item.findtext('pubDate') or '')
            if stamp.tzinfo is None:
                raise ValueError('RSS publication must be timezone-aware')
            if stamp > observed:
                continue
            link = item.findtext('link') or ''
            if not link.startswith('https://www.lemonde.fr/'):
                raise ValueError('RSS item link belongs to another publisher')
            observations.append(Observation(evidence_id=f'rss-{ticker}-{i}', entity_id=ticker, kind='event',
                claim=(title + ': ' + summary)[:500], provider_record_id=(item.findtext('guid') or item.findtext('link') or str(i)).rsplit('/', 1)[-1][:120],
                request_key=source_id, source_url=endpoint, article_url=link, publication_basis='source_declared_timestamp',
                published_at=stamp, event_time=stamp, retrieved_at=observed, statement_kind='reported_claim'))
        notes.append(f'RSS bounded search: {len(items)} items inspected, {len(observations)} keyword matches; summaries only.')
        transport.append({'key': endpoint, 'status': 'retrieved', 'attempts': meta.get('attempts')})
    return Collection(collection_id=uuid.uuid4().hex, source_id=source_id, endpoint=profile['endpoint'],
        capture_mode='network', source_class=profile['source_class'], coverage_mode='historical_filings',
        requested_ticker=ticker, collected_at=now or datetime.now(timezone.utc), entity=entity,
        observations=observations, license=snapshot, transport=transport, filing_discovery=discovery,
        sec_contact_configured=source_id == 'sec-submissions-history',
        retention='research_input_allowed',
        notes=notes + ['No raw responses, article bodies or response hashes are retained.'])
